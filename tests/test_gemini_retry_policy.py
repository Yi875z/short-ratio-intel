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
        # 中身のある応答とみなされる最小の形（結論あり・注目業種3件）。空に近い応答の扱いは別テスト。
        from types import SimpleNamespace

        monkeypatch.setattr(
            client,
            "_parse_response",
            lambda raw: SimpleNamespace(executive_summary=f"parsed:{raw}", top_sectors_analysis=[1, 2, 3]),
        )
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


def test_output_budget_comes_from_settings_and_stays_small(build_client):
    """出力上限は settings の1箇所。大きな出力枠は混雑時に 503 で落とされやすい（2026-10-01）。"""
    client, fake, _ = build_client(["{}"])
    _generate(client)

    budget = fake.calls[0]["kwargs"]["generation_config"]["max_output_tokens"]
    assert budget == gc.GEMINI_MAX_OUTPUT_TOKENS
    assert budget <= 16000, "32768 に戻すと混雑時に 503 で落ちやすい（PROJECT_RULES 参照）"


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
        def __init__(self, **kwargs):
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


def _workflow_text(name: str) -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _fetch_job_minutes(workflow: str) -> int:
    """fetch ジョブの timeout-minutes。先頭の guard ジョブ(5分)を拾わないよう fetch 以降を読む。"""
    import re

    fetch_section = workflow.split("\n  fetch:\n", 1)[1]
    return int(re.search(r"^\s*timeout-minutes:\s*(\d+)", fetch_section, re.M).group(1))


# 取得・分析など AI 生成より前の処理時間。2026-09-29 の本番ログで約6分。
PIPELINE_PREP_MINUTES = 8


def test_job_timeout_covers_the_whole_ai_budget():
    """GEMINI_TOTAL_BUDGET_SEC を伸ばすなら workflow の timeout-minutes も伸ばすこと。

    job 側が短いと退避モデルや2巡目へ到達する前に打ち切られ、自動退避が働かないまま
    レポートが欠落する（2026-08-24 の欠落と同じ結果になる）。設定が片方だけ
    動くのを防ぐため、AI 生成の時間予算＋取得処理と job の上限を突き合わせる。
    """
    import config.settings as settings

    job_minutes = _fetch_job_minutes(_workflow_text("daily_fetch.yml"))
    needed = settings.GEMINI_TOTAL_BUDGET_SEC / 60 + PIPELINE_PREP_MINUTES

    assert job_minutes >= needed, (
        f"job の上限 {job_minutes}分 < AI予算 {settings.GEMINI_TOTAL_BUDGET_SEC / 60:.0f}分"
        f" + 取得処理 {PIPELINE_PREP_MINUTES}分"
    )


def test_budget_keeps_the_first_round_unchanged():
    """時間予算は1巡目（全モデル×MAX_RETRIES×タイムアウト）を削ってはいけない。

    予算が短いと、2巡目のための仕組みが1巡目の退避先を切り捨てることになる。
    """
    import config.settings as settings

    chain = [settings.GEMINI_MODEL] + [
        m for m in settings.GEMINI_FALLBACK_MODELS if m != settings.GEMINI_MODEL
    ]
    retries = gc.GeminiReportGenerator.MAX_RETRIES
    backoff = sum(2 ** a for a in range(retries - 1))   # 503 時の同一モデル再試行待ち
    first_round_worst = len(chain) * (retries * settings.GEMINI_REQUEST_TIMEOUT_SEC + backoff)

    assert settings.GEMINI_TOTAL_BUDGET_SEC >= first_round_worst


# ──────────────────────────────────────────────────────────────
# 全モデルが 503 で落ちたときの巡回やり直し（2026-09-30 追加）
# 9/24・9/29 は 3モデル×3回を約3分で撃ち尽くし、混雑が引く前に諦めてレポートが欠落した。
# ──────────────────────────────────────────────────────────────
HIGH_DEMAND = RuntimeError(
    "503 This model is currently experiencing high demand. "
    "Spikes in demand are usually temporary. Please try again later."
)


def _patch_clock(monkeypatch):
    """time.sleep で進む偽の時計。予算判定を実時間に依存させない。"""
    clock = {"now": 0.0}
    slept: list[float] = []

    def _sleep(sec):
        slept.append(sec)
        clock["now"] += sec

    monkeypatch.setattr(gc.time, "sleep", _sleep)
    monkeypatch.setattr(gc.time, "monotonic", lambda: clock["now"])
    return clock, slept


def test_single_round_is_the_default(build_client):
    """画面の手動生成（引数なし）は従来どおり1巡で諦める。数分待たせない。"""
    client, fake, _ = build_client([HIGH_DEMAND] * 6)

    with pytest.raises(RuntimeError, match="503"):
        _generate(client)

    assert len(fake.calls) == 6          # 2モデル × 3回
    assert client.max_rounds == 1


def test_pipeline_waits_and_retries_the_chain_after_all_503(build_client, monkeypatch):
    """全モデルが 503 なら待ってからもう一巡し、2巡目は各モデル1回だけ試す。"""
    client, fake, _ = build_client([HIGH_DEMAND] * 6 + [HIGH_DEMAND, "{}"])
    client.max_rounds = 3
    _, slept = _patch_clock(monkeypatch)

    _generate(client)

    models = [c["model"] for c in fake.calls]
    assert models == ["model-primary"] * 3 + ["model-backup"] * 3 + ["model-primary", "model-backup"]
    assert gc.GEMINI_ROUND_WAIT_SEC in slept
    assert client.model_name == "model-backup"   # 実際に書いたモデルを記録できる


def test_second_round_skips_models_whose_daily_quota_is_gone(build_client, monkeypatch):
    """日次枠が尽きたモデルは2巡目でも叩かない（待っても今日は回復しない）。"""
    client, fake, _ = build_client([RuntimeError(DAILY_QUOTA_ERROR)] + [HIGH_DEMAND] * 3 + ["{}"])
    client.max_rounds = 2
    _patch_clock(monkeypatch)

    _generate(client)

    assert [c["model"] for c in fake.calls] == (
        ["model-primary"] + ["model-backup"] * 3 + ["model-backup"]
    )


def test_rounds_stop_before_exceeding_the_time_budget(build_client, monkeypatch):
    """次の1リクエストを始めると予算を超えるなら、巡回せずに打ち切る（job が先に切れないように）。"""
    client, fake, _ = build_client([HIGH_DEMAND] * 100)
    client.max_rounds = 50
    _, slept = _patch_clock(monkeypatch)

    with pytest.raises(RuntimeError, match="503"):
        _generate(client)

    total = sum(slept)
    assert total + gc.GEMINI_REQUEST_TIMEOUT_SEC <= gc.GEMINI_TOTAL_BUDGET_SEC
    assert len(fake.calls) < 100


def test_empty_report_falls_back_to_next_model(build_client, monkeypatch):
    """`{}` のような空に近い応答は成功として保存せず、次のモデルへ回す（独立レビュー #6）。"""
    from types import SimpleNamespace

    client, fake, _ = build_client(["EMPTY", "EMPTY", "EMPTY", "FULL"])
    monkeypatch.setattr(
        client,
        "_parse_response",
        lambda raw: SimpleNamespace(
            executive_summary="" if raw == "EMPTY" else "結論",
            top_sectors_analysis=[] if raw == "EMPTY" else [1, 2, 3],
        ),
    )
    _generate(client)

    assert [c["model"] for c in fake.calls] == ["model-primary"] * 3 + ["model-backup"]
    assert client.model_name == "model-backup"


def test_parse_error_is_not_retried_in_later_rounds(build_client, monkeypatch):
    """パース失敗はモデルを変えても待っても直らないので、巡回し直さない。"""
    parse_error = ValueError("1 validation error for ReadingReport")
    client, fake, _ = build_client([parse_error] * 3)
    client.max_rounds = 3
    _, slept = _patch_clock(monkeypatch)

    with pytest.raises(ValueError):
        _generate(client)

    assert len(fake.calls) == 3
    assert gc.GEMINI_ROUND_WAIT_SEC not in slept


def test_pipeline_uses_multiple_rounds(monkeypatch):
    """定時パイプラインだけが巡回やり直しを有効にする。"""
    import scripts.fetch_short_ratio as pipeline

    captured: dict = {}

    class _StubGenerator:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model_name = "m"

        def generate_report(self, *args, **kwargs):
            class _R:
                current_macro_context = ""

                def model_dump_json(self):
                    return "{}"

            return _R(), "#"

    monkeypatch.setattr(pipeline, "GeminiReportGenerator", _StubGenerator)
    monkeypatch.setattr(pipeline, "save_ai_report", lambda *a, **kw: None)

    pipeline._step_report("2026-09-29", {}, None, [], False)

    assert captured["max_rounds"] == pipeline.GEMINI_PIPELINE_MAX_ROUNDS
    assert pipeline.GEMINI_PIPELINE_MAX_ROUNDS > 1


# ──────────────────────────────────────────────────────────────
# Worker の重複起動ガード（2026-09-30 追加）
# ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["daily_fetch.yml", "us_daily_fetch.yml"])
def test_workflow_guards_duplicate_worker_dispatch(name):
    """Worker 由来の起動だけを重複判定し、人の手動実行は常に通す。"""
    workflow = _workflow_text(name)

    assert "source:" in workflow and "default: manual" in workflow
    assert '[ "${{ inputs.source }}" != "worker" ]' in workflow
    # 本処理は guard の判定に従う
    fetch_section = workflow.split("\n  fetch:\n", 1)[1]
    assert "needs: guard" in fetch_section
    assert "needs.guard.outputs.skip != 'true'" in fetch_section
    # 自分より前の run だけを数える（自分を数えると必ずスキップ、!= だと同時起動の2本が両方止まる）
    assert "select(.id < ${GITHUB_RUN_ID})" in workflow
    # 照会に失敗したら実行する側に倒し、guard の失敗で本処理が消えないようにする（独立レビュー #7）
    assert "!cancelled()" in fetch_section
    assert "照会に失敗。重複判定せずに実行する" in workflow
    # workflow_dispatch は Worker の入口。消さない
    assert "workflow_dispatch:" in workflow
