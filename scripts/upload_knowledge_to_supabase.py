"""
scripts/upload_knowledge_to_supabase.py

ローカルの外部ナレッジ（思考データ）を Supabase の knowledge_documents テーブルに
アップロード／更新する。公開リポジトリにファイルを置かずに、Streamlit Cloud と
GitHub Actions の両方からナレッジを参照できるようにするための一回／随時実行スクリプト。

前提:
    - 環境変数 DATABASE_URL に Supabase の接続文字列が設定されていること
    - ナレッジ原本は config.settings.EXTERNAL_KNOWLEDGE_DIR（既定 C:\\CarSol\\knowledgefile）

使い方:
    python -m scripts.upload_knowledge_to_supabase            # 全ファイルをupsert
    python -m scripts.upload_knowledge_to_supabase --list     # DB登録済みkeyを確認
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

from loguru import logger

from config.settings import EXTERNAL_KNOWLEDGE_DIR
from src.knowledge.loader import EXTERNAL_KNOWLEDGE_FILES
from src.storage.db import (
    get_knowledge_document_keys,
    upsert_knowledge_document,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="外部ナレッジを Supabase にアップロード")
    parser.add_argument("--list", action="store_true", help="DB登録済みkeyを表示して終了")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL が未設定です。Supabase 接続文字列を設定してください。")
        sys.exit(1)

    if args.list:
        keys = get_knowledge_document_keys()
        logger.info(f"DB登録済みナレッジ: {keys}")
        return

    uploaded = 0
    for key, filename in EXTERNAL_KNOWLEDGE_FILES.items():
        path = EXTERNAL_KNOWLEDGE_DIR / filename
        if not path.exists():
            logger.warning(f"スキップ（ファイルなし）: {key} ({path})")
            continue
        content = path.read_text(encoding="utf-8")
        upsert_knowledge_document(key, content, filename=filename)
        logger.success(f"アップロード: {key} <- {filename} ({len(content)}文字)")
        uploaded += 1

    logger.success(f"完了: {uploaded}件のナレッジを Supabase に保存しました")
    logger.info(f"DB登録済みkey: {get_knowledge_document_keys()}")


if __name__ == "__main__":
    main()
