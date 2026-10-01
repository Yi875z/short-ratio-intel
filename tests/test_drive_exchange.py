"""Google Drive 受け渡し（dots 方式）のテスト。Drive API は呼ばず、偽のサービスで振る舞いを固定する。"""
import pytest

from src.storage import drive_exchange as dx


class _FakeDrive:
    """files().list/create/update/get_media の最小の偽物。名前と親フォルダでファイルを管理する。"""

    def __init__(self):
        self.files_by_id: dict[str, dict] = {}
        self._next = 0

    # googleapiclient の呼び出し形に合わせる
    def files(self):
        return self

    def list(self, q, fields, pageSize):
        name = q.split("name = '")[1].split("'")[0]
        parent = q.split("' in parents")[0].split("'")[-1] if "in parents" in q else None
        hits = [
            {"id": fid} for fid, f in self.files_by_id.items()
            if f["name"] == name and (parent is None or parent in f["parents"])
        ]
        return _Exec({"files": hits})

    def create(self, body, fields, media_body=None):
        self._next += 1
        fid = f"id{self._next}"
        self.files_by_id[fid] = {
            "name": body["name"], "parents": body.get("parents", []),
            "content": _read(media_body),
        }
        return _Exec({"id": fid})

    def update(self, fileId, media_body):
        self.files_by_id[fileId]["content"] = _read(media_body)
        return _Exec({"id": fileId})

    def get_media(self, fileId):
        return self.files_by_id[fileId]["content"]


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _read(media):
    if media is None:
        return b""
    media._fd.seek(0)
    return media._fd.read()


class _FakeDownloader:
    def __init__(self, buf, content):
        buf.write(content)

    def next_chunk(self):
        return None, True


@pytest.fixture
def fake_drive(monkeypatch):
    fake = _FakeDrive()
    monkeypatch.setattr(dx, "_service", lambda: fake)
    import googleapiclient.http as http

    monkeypatch.setattr(http, "MediaIoBaseDownload", _FakeDownloader)
    return fake


def _content(fake, name):
    return next(f["content"] for f in fake.files_by_id.values() if f["name"] == name)


def test_publish_creates_bundle_instructions_and_empty_output(fake_drive):
    dx.publish_bundle("2026-10-01", "材料", "手順書")

    names = {f["name"] for f in fake_drive.files_by_id.values()}
    assert {dx.AGENT_FOLDER_NAME, "inputs", "outputs", "2026-10-01_input.md",
            "README_dots_task.md", "2026-10-01_report.json"} <= names
    assert _content(fake_drive, "2026-10-01_report.json") == b""   # dots が書き込む空ファイル
    assert _content(fake_drive, "2026-10-01_input.md").decode() == "材料"


def test_republishing_does_not_wipe_agent_output(fake_drive):
    """材料を置き直しても、dots が書いた出力を空で潰さない。"""
    dx.publish_bundle("2026-10-01", "材料", "手順書")
    out_id = next(fid for fid, f in fake_drive.files_by_id.items() if f["name"] == "2026-10-01_report.json")
    fake_drive.files_by_id[out_id]["content"] = b'{"executive_summary": "x"}'

    dx.publish_bundle("2026-10-01", "材料v2", "手順書")
    assert fake_drive.files_by_id[out_id]["content"] == b'{"executive_summary": "x"}'
    assert _content(fake_drive, "2026-10-01_input.md").decode() == "材料v2"


def test_fetch_returns_none_until_agent_writes(fake_drive):
    dx.publish_bundle("2026-10-01", "材料", "手順書")
    assert dx.fetch_agent_output("2026-10-01") is None            # 空のまま＝未記入

    out_id = next(fid for fid, f in fake_drive.files_by_id.items() if f["name"] == "2026-10-01_report.json")
    fake_drive.files_by_id[out_id]["content"] = '{"a": 1}'.encode("utf-8")
    assert dx.fetch_agent_output("2026-10-01") == '{"a": 1}'


def test_not_configured_raises_specific_error(monkeypatch):
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    assert not dx.is_configured()
    with pytest.raises(dx.DriveNotConfigured):
        dx._service()


def test_publish_script_is_fail_soft_without_credentials(monkeypatch):
    """資格情報が無くても日次パイプラインを止めない（終了コード0）。"""
    import scripts.publish_agent_bundle as pub

    def boom(*a, **k):
        raise dx.DriveNotConfigured("未設定")

    monkeypatch.setattr(pub.drive_exchange, "publish_bundle", boom)
    monkeypatch.setattr(pub, "build_agent_bundle", lambda *a, **k: "材料")
    monkeypatch.setattr("scripts.fetch_short_ratio._prepare_analysis", lambda d: (None, {}, None, [], None))
    monkeypatch.setattr("sys.argv", ["publish_agent_bundle", "--date", "2026-10-01"])
    assert pub.main() == 0


def test_agent_instructions_require_writing_into_the_prepared_file():
    """drive.file 権限では dots が新しく作ったファイルは見えない。既存の空ファイルに書かせる。"""
    from src.ai_engine.agent_bundle import INPUT_END_MARKER, build_agent_instructions

    text = build_agent_instructions("short_ratio_agent")
    assert "新しいファイルを作らず" in text
    assert "short_ratio_agent/outputs" in text
    assert INPUT_END_MARKER in text


def _workflow(name):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_daily_workflow_publishes_bundle_even_if_ai_report_failed():
    """Gemini が全滅した日こそ dots に書かせたいので、材料の配置は AI の失敗に関わらず走らせる。"""
    wf = _workflow("daily_fetch.yml")
    step = wf.split("- name: Publish bundle for dots", 1)[1].split("- name:", 1)[0]
    assert "!cancelled()" in step
    assert "scripts.publish_agent_bundle" in step
    assert "GOOGLE_OAUTH_REFRESH_TOKEN: ${{ secrets.GOOGLE_OAUTH_REFRESH_TOKEN }}" in wf


def test_import_workflow_accepts_worker_source_input():
    """Worker は全ジョブに source=worker を渡す。入力に無いと 422 で起動できない。"""
    wf = _workflow("agent_report_import.yml")
    assert "source:" in wf and "default: manual" in wf
    assert "--from-drive" in wf
    assert "workflow_dispatch:" in wf
