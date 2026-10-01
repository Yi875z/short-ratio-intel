"""
scripts/google_oauth_setup.py

GitHub Actions が Google Drive の受け渡し用フォルダを読み書きするための許可（リフレッシュトークン）を、
利用者のPCで一度だけ取得する。

前提:
    1. Google Cloud で OAuth クライアント（種類: デスクトップ アプリ）を作り、JSON を
       `.secrets/google_oauth_client.json` に保存してある（`.secrets/` は .gitignore 済み）
    2. 手元に google-auth-oauthlib がある（本番の依存には入れない。`pip install google-auth-oauthlib`）

実行するとブラウザが開き、Google アカウントで許可する。権限は drive.file
（このアプリが作ったファイルだけ）。結果は `.secrets/google_oauth_token.json` に保存する。
画面やログにはトークンを出さない。

GitHub Secrets への登録（ファイルから直接パイプで入れる。対話の貼り付けは空登録の罠がある）:
    python -m scripts.google_oauth_setup --print-secret GOOGLE_OAUTH_CLIENT_ID     | gh secret set GOOGLE_OAUTH_CLIENT_ID
    python -m scripts.google_oauth_setup --print-secret GOOGLE_OAUTH_CLIENT_SECRET | gh secret set GOOGLE_OAUTH_CLIENT_SECRET
    python -m scripts.google_oauth_setup --print-secret GOOGLE_OAUTH_REFRESH_TOKEN | gh secret set GOOGLE_OAUTH_REFRESH_TOKEN
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
CLIENT_FILE = SECRETS / "google_oauth_client.json"
TOKEN_FILE = SECRETS / "google_oauth_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _client() -> dict:
    data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
    return data.get("installed") or data.get("web") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Drive 受け渡し用の許可を一度だけ取得する")
    parser.add_argument("--print-secret", choices=[
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN",
    ], help="指定した値だけを標準出力へ出す（gh secret set へパイプする用）")
    args = parser.parse_args()

    if args.print_secret:
        client = _client()
        value = {
            "GOOGLE_OAUTH_CLIENT_ID": client.get("client_id", ""),
            "GOOGLE_OAUTH_CLIENT_SECRET": client.get("client_secret", ""),
            "GOOGLE_OAUTH_REFRESH_TOKEN": json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("refresh_token", ""),
        }[args.print_secret]
        if not value:
            print(f"{args.print_secret} が空です", file=sys.stderr)
            return 1
        sys.stdout.write(value)
        return 0

    if not CLIENT_FILE.exists():
        print(f"OAuth クライアントの JSON がありません: {CLIENT_FILE}", file=sys.stderr)
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    # access_type=offline と prompt=consent で、確実にリフレッシュトークンを受け取る
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("リフレッシュトークンを受け取れませんでした。もう一度実行してください。", file=sys.stderr)
        return 1
    SECRETS.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"refresh_token": creds.refresh_token}), encoding="utf-8")
    print(f"許可を保存しました: {TOKEN_FILE}（トークンは表示しません）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
