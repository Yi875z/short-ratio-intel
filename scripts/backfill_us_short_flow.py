"""
scripts/backfill_us_short_flow.py

米国ショートフローの履歴を一括取得して DB へ投入する（US-P1）。

FINRA CNMS はローリング約12ヶ月分が同一エンドポイントで取得できる。
Zスコアは過去60営業日の分布を使うため、初回に250営業日分を入れておけば
翌日から相対評価が有効になる。

接続先 DB は環境変数 DATABASE_URL で決まる:
    - DATABASE_URL 設定あり → Supabase(PostgreSQL)
    - DATABASE_URL 未設定   → ローカル SQLite（開発用）

使い方:
    python -m scripts.backfill_us_short_flow                    # 直近250営業日
    python -m scripts.backfill_us_short_flow --days 60          # 営業日数を指定
    python -m scripts.backfill_us_short_flow --from 2026-01-05 --to 2026-08-05
    python -m scripts.backfill_us_short_flow --dry-run          # DB書き込みなし
    python -m scripts.backfill_us_short_flow --skip-price       # 日足OHLCVを取得しない

⚠️ 日本側のパイプライン（scripts/fetch_short_ratio.py）とは完全に独立しており、
   本スクリプトは日本のテーブルに一切触れない。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

# `python scripts/backfill_us_short_flow.py` 直叩きでも import が通るようにする
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

from loguru import logger

from config.settings import US_BACKFILL_DAYS
from config.us_universe import US_UNIVERSE
from src.data_fetcher.finra_client import FinraShortVolumeClient, normalize_date
from src.data_fetcher.us_price_client import UsPriceClient
from src.storage.db import (
    upsert_us_market_daily_records,
    upsert_us_short_volume_records,
)

# 営業日N日ぶんを暦日に換算する係数（土日祝を見込んで多めに取る）
_CALENDAR_DAYS_PER_BUSINESS_DAY = 1.45


def _resolve_range(args: argparse.Namespace) -> tuple[str, str]:
    """--from/--to または --days から取得範囲（ISO日付）を決める。"""
    if args.to:
        end = date.fromisoformat(normalize_date(args.to))
    else:
        # FINRA の当日ファイルは米東部18時公開。前日までを既定の終端とする
        end = date.today() - timedelta(days=1)

    if args.from_date:
        start = date.fromisoformat(normalize_date(args.from_date))
    else:
        span = int(args.days * _CALENDAR_DAYS_PER_BUSINESS_DAY)
        start = end - timedelta(days=span)

    if start > end:
        raise ValueError(f"取得範囲が不正です: {start} → {end}")
    return start.isoformat(), end.isoformat()


def _backfill_short_volume(
    start: str,
    end: str,
    tickers: list[str],
    dry_run: bool,
) -> int:
    """FINRA CNMS を期間取得して保存する。保存件数を返す。"""
    logger.info(f"[1/2] FINRA CNMS を取得: {start} → {end} / {len(tickers)}銘柄")
    client = FinraShortVolumeClient()
    records = client.get_range_records(start, end, tickers=tickers)

    if not records:
        logger.warning("FINRA から1件も取得できませんでした")
        return 0

    if dry_run:
        logger.info(f"[dry-run] 保存をスキップ: {len(records)}レコード")
        return 0

    return upsert_us_short_volume_records(records)


def _backfill_prices(
    start: str,
    end: str,
    tickers: list[str],
    dry_run: bool,
) -> int:
    """Yahoo Finance の日足を取得して保存する。保存件数を返す。"""
    logger.info(f"[2/2] 日足OHLCVを取得: {start} → {end} / {len(tickers)}銘柄")
    client = UsPriceClient()
    records = client.get_daily_ohlcv_bulk(tickers, start, end)

    if not records:
        logger.warning("日足を1件も取得できませんでした")
        return 0

    if dry_run:
        logger.info(f"[dry-run] 保存をスキップ: {len(records)}レコード")
        return 0

    return upsert_us_market_daily_records(records)


def run(args: argparse.Namespace) -> int:
    backend = "Supabase(PostgreSQL)" if os.environ.get("DATABASE_URL") else "ローカルSQLite"
    start, end = _resolve_range(args)
    tickers = args.tickers.split(",") if args.tickers else US_UNIVERSE

    logger.info(
        f"米国ショートフロー バックフィル開始 | {start} → {end} | "
        f"DB={backend} | dry_run={args.dry_run}"
    )

    saved_short = _backfill_short_volume(start, end, tickers, args.dry_run)

    saved_price = 0
    if not args.skip_price:
        saved_price = _backfill_prices(start, end, tickers, args.dry_run)

    logger.success(
        f"バックフィル完了 | ショートボリューム {saved_short}件 / "
        f"日足 {saved_price}件 | DB={backend}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="米国ショートフロー（FINRA CNMS + 日足）のバックフィル"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=US_BACKFILL_DAYS,
        help=f"取得する直近営業日数（既定: {US_BACKFILL_DAYS}）",
    )
    parser.add_argument("--from", dest="from_date", help="取得開始日 YYYY-MM-DD")
    parser.add_argument("--to", help="取得終了日 YYYY-MM-DD（既定: 前日）")
    parser.add_argument(
        "--tickers",
        help="対象ティッカーをカンマ区切りで指定（既定: config/us_universe.py の全銘柄）",
    )
    parser.add_argument("--skip-price", action="store_true", help="日足OHLCVの取得をスキップ")
    parser.add_argument("--dry-run", action="store_true", help="DBへ書き込まない")
    parser.add_argument("--verbose", action="store_true", help="DEBUGログを出力")
    args = parser.parse_args()

    if args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")

    try:
        sys.exit(run(args))
    except Exception as exc:  # noqa: BLE001 失敗をログに残して非ゼロ終了
        logger.error(f"バックフィル失敗: {exc}")
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
