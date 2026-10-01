"""
Google Drive を介した、サブスクの AI エージェント（ChatGPT の dots 等）との受け渡し。

なぜ Drive か（2026-10-01）:
    dots は ChatGPT のクラウドで動き、Google Drive の読み書きができる（9/24・9/29 の試運転で実証）。
    システム側（GitHub Actions）も Drive API で読み書きすれば、PC を介さずに受け渡しできる。
    エージェントには DB の資格情報を渡さない。受け渡すのは材料と出力の2ファイルだけ。

なぜサービスアカウントではなく利用者本人の OAuth か:
    サービスアカウントには保存容量が無く、個人のマイドライブに新しいファイルを作れない
    （作れるのは Google Workspace の共有ドライブだけ）。利用者本人の許可（リフレッシュトークン）で動かし、
    ファイルは利用者の持ち物として作る。

なぜ権限が drive.file か:
    このアプリが作ったファイルにしか触れない最小の権限。トークンが漏れてもドライブの他のファイルは読めない。
    そのため、受け渡し用フォルダもアプリ自身が作り（AGENT_FOLDER_NAME）、出力用の空ファイルも
    材料と一緒に先に作っておく。エージェントはその空ファイルに書き込む。
    エージェントが別の新しいファイルを作ると、このアプリからは見えない（drive.file の仕様）。

必要な環境変数（GitHub Secrets / ローカルの .env）:
    GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN
    初回の取得は scripts/google_oauth_setup.py（利用者のPCで一度だけ）。
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from loguru import logger

DRIVE_SCOPE = os.getenv("GOOGLE_DRIVE_SCOPE", "https://www.googleapis.com/auth/drive.file")
AGENT_FOLDER_NAME = os.getenv("DRIVE_AGENT_FOLDER_NAME", "short_ratio_agent")
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveNotConfigured(RuntimeError):
    """OAuth の資格情報が未設定。受け渡しを使わずに続行する側で扱う。"""


def is_configured() -> bool:
    return all(os.getenv(k) for k in (
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"
    ))


def _service():
    if not is_configured():
        raise DriveNotConfigured("GOOGLE_OAUTH_* が未設定")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[DRIVE_SCOPE],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find(service, name: str, parent_id: str | None, mime: str | None = None) -> str | None:
    q = [f"name = '{_escape(name)}'", "trashed = false"]
    if parent_id:
        q.append(f"'{parent_id}' in parents")
    if mime:
        q.append(f"mimeType = '{mime}'")
    found = service.files().list(q=" and ".join(q), fields="files(id)", pageSize=2).execute()
    files = found.get("files", [])
    return files[0]["id"] if files else None


def _ensure_folder(service, name: str, parent_id: str | None = None) -> str:
    folder_id = _find(service, name, parent_id, FOLDER_MIME)
    if folder_id:
        return folder_id
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return service.files().create(body=body, fields="id").execute()["id"]


@dataclass(frozen=True)
class AgentFolders:
    root: str
    inputs: str
    outputs: str


def ensure_agent_folders(service=None) -> AgentFolders:
    """受け渡し用フォルダ（AGENT_FOLDER_NAME/inputs, /outputs）を探し、無ければ作る。"""
    service = service or _service()
    root = _ensure_folder(service, AGENT_FOLDER_NAME)
    return AgentFolders(
        root=root,
        inputs=_ensure_folder(service, "inputs", root),
        outputs=_ensure_folder(service, "outputs", root),
    )


def upload_text(folder_id: str, name: str, text: str, mime: str = "text/plain", service=None,
                overwrite: bool = True) -> str:
    """プレーンテキストのファイルを作る（同名があれば中身を差し替える。overwrite=False なら触らない）。"""
    from googleapiclient.http import MediaIoBaseUpload

    service = service or _service()
    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype=mime, resumable=False)
    existing = _find(service, name, folder_id)
    if existing:
        if overwrite:
            service.files().update(fileId=existing, media_body=media).execute()
        return existing
    body = {"name": name, "parents": [folder_id], "mimeType": mime}
    return service.files().create(body=body, media_body=media, fields="id").execute()["id"]


def download_text(folder_id: str, name: str, service=None) -> str | None:
    """ファイルの中身を文字列で返す。無ければ None。"""
    from googleapiclient.http import MediaIoBaseDownload

    service = service or _service()
    file_id = _find(service, name, folder_id)
    if not file_id:
        return None
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8-sig")


def input_name(report_date: str) -> str:
    return f"{report_date}_input.md"


def output_name(report_date: str) -> str:
    return f"{report_date}_report.json"


def publish_bundle(report_date: str, bundle_text: str, instructions: str) -> AgentFolders:
    """材料・指示書・出力用の空ファイルを置く。

    出力用の空ファイルは既にあれば触らない（エージェントが書いた後に空で潰さない）。
    """
    service = _service()
    folders = ensure_agent_folders(service)
    upload_text(folders.inputs, input_name(report_date), bundle_text, "text/markdown", service)
    upload_text(folders.root, "README_dots_task.md", instructions, "text/markdown", service)
    upload_text(folders.outputs, output_name(report_date), "", "application/json", service, overwrite=False)
    logger.info(f"Drive へ材料を配置: {AGENT_FOLDER_NAME}/inputs/{input_name(report_date)}")
    return folders


def fetch_agent_output(report_date: str) -> str | None:
    """エージェントが書いた出力を読む。空ファイルのまま（未記入）なら None。"""
    service = _service()
    folders = ensure_agent_folders(service)
    text = download_text(folders.outputs, output_name(report_date), service)
    if text is None or not text.strip():
        return None
    return text
