"""
scripts/backfill_market_breadth.py

騰落銘柄数（市場の広がり）と TOPIX 騰落率を J-Quants API から取得して DB に貯める。

初回はまとめてバックフィルし、以降は日次パイプラインが最新日を1日ぶん追加する想定。
同一 (date, market_scope) の再投入で行数は増えない（冪等）ので、途中で失敗しても
同じコマンドを再実行すれば続きから埋まる。

接続先 DB は環境変数 DATABASE_URL で決まる:
    - DATABASE_URL 設定あり → Supabase(PostgreSQL)
    - DATABASE_URL 未設定   → ローカル SQLite

使い方:
    python -m scripts.backfill_market_breadth                  # 直近1年
    python -m scripts.backfill_market_breadth --days 60        # 直近60営業日
    python -m scripts.backfill_market_breadth --from 2026-01-01 --to 2026-08-28
    python -m scripts.backfill_market_breadth --force          # 保存済みの日も取り直す
    python -m scripts.backfill_market_breadth --dry-run        # DBに書かず件数だけ出す

リクエスト数の目安:
    連続営業日を古い順に処理し、前営業日の日足は直前ループの結果を使い回すため、
    1営業日あたり2リクエスト（日足＋銘柄一覧）＋ TOPIX を期間で1回。
    1年（約245営業日）で約490リクエスト。Light の 60req/分に対して最短間隔を
    空けているので所要は10分弱。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# `python scripts/backfill_market_breadth.py` 直叩きでも import が通るようにする
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

from loguru import logger

from src.analyzer.market_breadth import compute_all_breadth, compute_topix_change
from src.data_fetcher.jquants_api_client import (
    JQuantsApiClient,
    JQuantsError,
    JQuantsNotConfiguredError,
)
from src.storage.db import (
    get_saved_market_breadth_dates,
    upsert_market_breadth_records,
)

DEFAULT_DAYS = 245  # 約1年ぶんの営業日


def _resolve_range(args: argparse.Namespace) -> tuple[str, str]:
    """処理対象のカレンダー期間（from, to）を決める。"""
    if args.to:
        end = datetime.strptime(args.to, "%Y-%m-%d")
    else:
        end = datetime.now()

    if args.from_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d")
    else:
        # 営業日数から逆算する。休場を含むぶん多めに遡ってからカレンダーで絞る。
        start = end - timedelta(days=int(args.days * 1.6) + 10)

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _business_days(client: JQuantsApiClient, from_date: str, to_date: str) -> list[str]:
    """公式の取引カレンダーから営業日を古い順に返す。"""
    rows = client.get_trading_calendar(from_date, to_date)
    return sorted(
        row["Date"]
        for row in rows
        if row.get("Date") and str(row.get("HolDiv")) == "1"
    )


def run(args: argparse.Namespace) -> int:
    backend = "Supabase(PostgreSQL)" if os.environ.get("DATABASE_URL") else "ローカルSQLite"
    client = JQuantsApiClient()
    if not client.is_configured:
        raise JQuantsNotConfiguredError(
            "JQUANTS_API_KEY が未設定です。.env か環境変数に設定してください。"
        )

    from_date, to_date = _resolve_range(args)
    logger.info(f"バックフィル開始 | DB={backend} | 期間 {from_date} 〜 {to_date}")

    business_days = _business_days(client, from_date, to_date)
    if not args.from_date:
        # 先頭日は前日比較の供給元にしか使わないので、要求日数＋1日ぶん確保する
        business_days = business_days[-(args.days + 1):]
    if len(business_days) < 2:
        raise RuntimeError(
            f"営業日が足りません（{len(business_days)}日）。前日比較には最低2営業日が要ります。"
        )

    # 先頭日は「その前営業日」が範囲外なので、前日データの供給元としてだけ使う。
    target_days = business_days[1:]
    logger.info(f"対象営業日: {len(target_days)}日（{target_days[0]} 〜 {target_days[-1]}）")

    already_saved = set() if args.force else set(get_saved_market_breadth_dates())
    if already_saved:
        logger.info(f"保存済みをスキップします（{len(already_saved)}日ぶん保存済み）")

    # TOPIX は期間まとめて1回。前営業日は「時系列の直前の足」で解決する。
    topix_bars = client.get_topix_bars(business_days[0], business_days[-1])

    prev_date = business_days[0]
    prev_bars = None
    saved_total = 0
    skipped = 0
    failed: list[str] = []

    for index, target_date in enumerate(target_days, start=1):
        if target_date in already_saved:
            skipped += 1
            # スキップしても翌日の前日データが要るので、日足のキャッシュだけ捨てる。
            prev_date, prev_bars = target_date, None
            continue

        try:
            if prev_bars is None:
                prev_bars = client.get_daily_bars(prev_date)

            bars_today = client.get_daily_bars(target_date)
            master = client.get_listed_master(target_date)

            breadth = compute_all_breadth(target_date, bars_today, prev_bars, master)
            topix = compute_topix_change(topix_bars, target_date)

            records = []
            for counts in breadth.values():
                record = counts.to_dict()
                if topix is not None:
                    record["topix_close"] = topix.close
                    record["topix_prev_close"] = topix.prev_close
                    record["topix_change_pct"] = topix.change_pct
                records.append(record)

            if args.dry_run:
                prime = breadth.get("TSE_PRIME")
                logger.info(
                    f"[{index}/{len(target_days)}] {target_date} (dry-run) "
                    f"プライム 上={prime.advancing} 下={prime.declining} "
                    f"TOPIX={topix.change_pct if topix else None}"
                )
            else:
                saved_total += upsert_market_breadth_records(records)
                if index % 20 == 0 or index == len(target_days):
                    logger.info(f"[{index}/{len(target_days)}] {target_date} まで保存")

        except JQuantsError as exc:
            # 1日失敗しても全体を止めない。冪等なので再実行で埋まる。
            logger.warning(f"{target_date} の取得に失敗: {exc}")
            failed.append(target_date)
            bars_today = None

        # 次のループの前日データとして使い回す（連続営業日ならリクエストが半分になる）。
        # 失敗した日は None なので、次の日は前日ぶんを取り直す。
        prev_date = target_date
        prev_bars = bars_today

    logger.success(
        f"バックフィル完了 | 保存 {saved_total}件 / スキップ {skipped}日 / 失敗 {len(failed)}日"
    )
    if failed:
        logger.warning(f"失敗した営業日: {failed}")
        logger.warning("同じコマンドを再実行すれば冪等に埋まります。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="騰落銘柄数・TOPIX騰落率のバックフィル（J-Quants API v2）"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"--from 未指定時に遡る営業日数（既定: {DEFAULT_DAYS}＝約1年）",
    )
    parser.add_argument("--from", dest="from_date", help="開始日 YYYY-MM-DD")
    parser.add_argument("--to", help="終了日 YYYY-MM-DD（既定: 本日）")
    parser.add_argument(
        "--force", action="store_true", help="保存済みの日付も取り直す",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="DBに書かず取得と算出だけ行う",
    )
    args = parser.parse_args()

    try:
        sys.exit(run(args))
    except Exception as exc:  # noqa: BLE001 失敗をログに残して非ゼロ終了
        logger.error(f"バックフィル失敗: {exc}")
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
