"""
scripts/migrate_sqlite_to_supabase.py

ローカル SQLite (data/short_ratio.db) の全レコードを
Supabase(PostgreSQL) へ一度だけコピーするワンタイム移行スクリプト。

前提:
    - 環境変数 DATABASE_URL に Supabase の Session pooler 接続文字列を設定しておく
      （postgresql://postgres.xxxx:[PASSWORD]@aws-...pooler.supabase.com:5432/postgres）
    - 移行元 SQLite は config.settings.DB_PATH（既定 data/short_ratio.db）

使い方（PowerShell 例）:
    $env:DATABASE_URL = "postgresql://...pooler.supabase.com:5432/postgres"
    python -m scripts.migrate_sqlite_to_supabase            # 実行
    python -m scripts.migrate_sqlite_to_supabase --dry-run  # 件数確認のみ

安全策:
    - 既にデータが入っている宛先テーブルは既定でスキップ（--force で上書き追記）
    - id は除外して INSERT し、Postgres 側の autoincrement に任せる
      （date 等の UNIQUE 制約で重複は弾かれる）
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
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from config.settings import DB_PATH
from src.storage.models import (
    AiReport,
    Base,
    MarketNewsSnapshot,
    MarketShortRatioDaily,
    MarketThemeSnapshot,
    ShortRatioDaily,
)

# 移行対象テーブル（依存関係はないので順不同で可）
MODELS = [
    ShortRatioDaily,
    MarketShortRatioDaily,
    AiReport,
    MarketThemeSnapshot,
    MarketNewsSnapshot,
]


def _row_to_dict(obj) -> dict:
    """ORM オブジェクトを、id を除いた列名→値の dict に変換する。"""
    return {
        col.name: getattr(obj, col.name)
        for col in obj.__table__.columns
        if col.name != "id"
    }


def _count(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → Supabase 移行")
    parser.add_argument("--dry-run", action="store_true", help="件数確認のみ（書き込まない）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="宛先テーブルにデータがあっても追記する（既定はスキップ）",
    )
    args = parser.parse_args()

    target_url = os.environ.get("DATABASE_URL") or ""
    if not target_url:
        logger.error("DATABASE_URL が未設定です。Supabase の接続文字列を設定してください。")
        sys.exit(1)
    if target_url.startswith("postgres://"):
        target_url = target_url.replace("postgres://", "postgresql://", 1)

    if not DB_PATH.exists():
        logger.error(f"移行元 SQLite が見つかりません: {DB_PATH}")
        sys.exit(1)

    source_engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
    target_engine = create_engine(target_url, echo=False, pool_pre_ping=True)

    logger.info(f"移行元: SQLite({DB_PATH})")
    logger.info("移行先: Supabase(PostgreSQL)")

    # 宛先にテーブルを作成（既存ならスキップされる）
    if not args.dry_run:
        Base.metadata.create_all(target_engine)

    total_copied = 0
    with Session(source_engine) as src, Session(target_engine) as dst:
        for model in MODELS:
            name = model.__tablename__
            src_count = _count(src, model)
            dst_count = _count(dst, model) if not args.dry_run else 0

            if args.dry_run:
                logger.info(f"[{name}] 移行元 {src_count} 件")
                continue

            if dst_count and not args.force:
                logger.warning(
                    f"[{name}] 宛先に既に {dst_count} 件あるためスキップ（--force で追記可）"
                )
                continue

            rows = src.execute(select(model)).scalars().all()
            for obj in rows:
                dst.add(model(**_row_to_dict(obj)))
            dst.commit()
            logger.success(f"[{name}] {len(rows)} 件をコピーしました")
            total_copied += len(rows)

    if args.dry_run:
        logger.info("dry-run 完了（書き込みなし）")
    else:
        logger.success(f"移行完了: 合計 {total_copied} 件をコピーしました")


if __name__ == "__main__":
    main()
