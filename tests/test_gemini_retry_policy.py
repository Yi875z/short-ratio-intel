"""
Gemini 呼び出しのリトライ／退避ポリシーの回帰テスト。

2026-08-24 の障害（AIレポート欠落）を再発させないためのテスト。
遅いモデルで 504 → SDK 内部リトライが日次枠(RPD)を食い潰す →
定時実行が 429 で全滅、という連鎖を防ぐ2点を固定する。

1. generate_content に `retry=None` を渡し「1呼び出し = 1リクエスト」にすること
2. 日次枠の 429 は待っても回復しないので、リトライせず退避モデルへ移ること
"""
import pytest

import src.ai_engine.gemini_client as gc


DAILY_QUOTA_ERROR = (
    "429 You exceeded your current quota. "
    '* Quota exceeded for metric: generate_content_free_tier_requests, limit: 20 '
    'violations { quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier" }'
)
RATE_LIMIT_ERROR = (
    "429 You exceeded your current quota. "
    'violations { quota_id: "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" }'
)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FakeModel:
    def __init__(self, model_name, system_instruction=None):
        self.model_name = model_name
        self._log = None  # _FakeGenai から注入される

    def generate_content(self, prompt, **kwargs):
        self._log.calls.append({"model": self.model_name, "kwargs": kwargs})
        outcome = self._log.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeGenai:
    """genai モジュールの差し替え。生成されたモデル名と呼び出しを記録する。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.built_models: list[str] = []

    def configure(self, **kwargs):
        pass

    def GenerationConfig(self, **kwargs):
        return kwargs

    def GenerativeModel(self, model_name, system_instruction=None):
        self.built_models.append(model_name)
        model = _FakeModel(model_name, system_instruction)
        model._log = self
        return model


@pytest.fixture
def build_client(monkeypatch):
    """外部依存を全て差し替えた GeminiReportGenerator を返すファクトリ"""

    def _factory(outcomes, model="model-primary", fallbacks=("model-backup",)):
        fake = _FakeGenai(outcomes)
        monkeypatch.setattr(gc, "genai", fake)
        monkeypatch.setattr(gc, "GEMINI_API_KEY", "dummy-key")
        monkeypatch.setattr(gc, "GEMINI_MODEL", model)
        monkeypatch.setattr(gc, "GEMINI_FALLBACK_MODELS", list(fallbacks))
        monkeypatch.setattr(gc, "build_system_prompt", lambda: "SYSTEM")
        monkeypatch.setattr(gc, "build_user_prompt", lambda *a, **k: "USER")
        monkeypatch.setattr(gc, "lint_report_markdown", lambda *a, **k: [])

        slept: list[float] = []
        monkeypatch.setattr(gc.time, "sleep", lambda s: slept.append(s))

        client = gc.GeminiReportGenerator()
        monkeypatch.setattr(client, "_parse_response", lambda raw: f"parsed:{raw}")
        monkeypatch.setattr(client, "_render_markdown", lambda obj, date: f"md:{obj}")
        return client, fake, slept

    return _factory


def _generate(client):
    return client.generate_report("2026-08-24", {}, None, [])


@pytest.mark.parametrize(
    "message, expected",
    [
        (DAILY_QUOTA_ERROR, "daily_quota"),
        (RATE_LIMIT_ERROR, "rate_limit"),
        ("504 Deadline expired before operation could complete.", "api"),
        ("503 Service Unavailable", "api"),
        ("1 validation error for ReadingReport", "other"),
    ],
)
def test_classify_error(message, expected):
    assert gc.GeminiReportGenerator._classify_error(message) == expected


def test_sdk_internal_retry_is_disabled(build_client):
    """SDK 内部リトライを切らないと1呼び出しが日次枠を何十も消費する"""
    client, fake, _ = build_client(["{}"])
    _generate(client)

    options = fake.calls[0]["kwargs"]["request_options"]
    assert options["retry"] is None
    assert options["timeout"] == gc.GEMINI_REQUEST_TIMEOUT_SEC


def test_daily_quota_switches_model_without_waiting(build_client):
    """日次枠の枯渇は待っても回復しない → 同一モデルへの再試行はせず退避する"""
    client, fake, slept = build_client([RuntimeError(DAILY_QUOTA_ERROR), "{}"])
    _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary", "model-backup"]
    assert fake.built_models == ["model-primary", "model-backup"]
    assert slept == []           # 65秒待ちを挟まない
    assert client.model_name == "model-backup"


def test_rate_limit_waits_on_same_model(build_client):
    """分次レートの 429 は 60秒超待てば同じモデルで回復する"""
    client, fake, slept = build_client([RuntimeError(RATE_LIMIT_ERROR), "{}"])
    _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary", "model-primary"]
    assert slept == [65]
    assert fake.built_models == ["model-primary"]


def test_api_error_retries_then_falls_back(build_client):
    """504 等はまず同一モデルで粘り、尽きたら退避モデルへ移る"""
    deadline = RuntimeError("504 Deadline expired before operation could complete.")
    client, fake, _ = build_client([deadline, deadline, deadline, "{}"])
    _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary"] * 3 + ["model-backup"]


def test_parse_error_does_not_burn_fallback_models(build_client):
    """モデルを変えても直らない種類のエラーで退避モデルの枠まで潰さない"""
    parse_error = ValueError("1 validation error for ReadingReport")
    client, fake, _ = build_client([parse_error] * gc.GeminiReportGenerator.MAX_RETRIES)

    with pytest.raises(ValueError):
        _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary"] * 3
    assert fake.built_models == ["model-primary"]


def test_all_models_exhausted_raises_last_error(build_client):
    """全モデルが日次枠切れなら、最後の例外をそのまま送出して非ゼロ終了させる"""
    client, fake, _ = build_client([RuntimeError(DAILY_QUOTA_ERROR)] * 2)

    with pytest.raises(RuntimeError, match="PerDay"):
        _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary", "model-backup"]


# ──────────────────────────────────────────────────────────────
# モデル指定の出所（2026-08-24: Streamlit Cloud Secrets だけ古い値が残り、
# 手動生成と定時実行で別モデルが動いていたのに気づけなかった）
# ──────────────────────────────────────────────────────────────
def _reload_settings():
    import importlib

    import config.settings as settings

    return importlib.reload(settings)


def test_model_default_is_single_source(monkeypatch):
    """環境変数が無ければリポジトリ既定が使われ、上書きフラグは立たない"""
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = _reload_settings()
    try:
        assert settings.GEMINI_MODEL == settings.GEMINI_MODEL_DEFAULT
        assert settings.GEMINI_MODEL_IS_OVERRIDDEN is False
    finally:
        _reload_settings()


def test_env_override_is_detected(monkeypatch):
    """Secrets 等で上書きされたら検知できる（警告表示の根拠）"""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    settings = _reload_settings()
    try:
        assert settings.GEMINI_MODEL == "gemini-3.5-flash"
        assert settings.GEMINI_MODEL_IS_OVERRIDDEN is True
    finally:
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        _reload_settings()


def test_workflow_does_not_pin_the_model():
    """daily_fetch.yml に GEMINI_MODEL の env を復活させない（二重管理の防止）"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "daily_fetch.yml").read_text(encoding="utf-8")
    active = [
        line for line in workflow.splitlines()
        if "GEMINI_MODEL" in line and not line.strip().startswith("#")
    ]
    assert active == [], f"workflow がモデルを固定している: {active}"


# ──────────────────────────────────────────────────────────────
# 保存されるモデル名（自動退避が起きた日を後から追えるようにする）
# ──────────────────────────────────────────────────────────────
def test_pipeline_records_the_model_actually_used(monkeypatch):
    """退避が起きたら、設定値ではなく実際に使われたモデルを DB に記録する"""
    import scripts.fetch_short_ratio as pipeline

    class _StubGenerator:
        def __init__(self):
            self.model_name = "model-primary"

        def generate_report(self, *args, **kwargs):
            self.model_name = "model-backup"    # 日次枠枯渇で退避したとみなす
            return _StubReport(), "# レポート本文"

    class _StubReport:
        current_macro_context = "マクロ"

        def model_dump_json(self):
            return "{}"

    saved: dict = {}
    monkeypatch.setattr(pipeline, "GeminiReportGenerator", _StubGenerator)
    monkeypatch.setattr(
        pipeline,
        "save_ai_report",
        lambda *a, **kw: saved.update(kw),
    )

    chars, report_obj, used_model = pipeline._step_report("2026-08-24", {}, None, [], False)

    assert used_model == "model-backup"
    assert saved["model_used"] == "model-backup"
    assert chars == len("# レポート本文")
