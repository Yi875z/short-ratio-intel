"""
scripts/backfill_sector_flow_features.py

業種別フロー特徴量（Phase 0）を J-Quants から取得して DB に貯める。

⚠️ このスクリプトは**判定を一切行わない**。特徴量と将来リターンを並べて
   保存するだけ。状態分類は、効く特徴量が判明してから最小限だけ作る。

将来リターンは特徴量とは別パスで埋める（`--forward-only` で更新のみ実行可）。
特徴量の取り直しで既に埋めた将来リターンが消えないよう、保存関数側で
fwd_* 列を上書きしない作りにしてある。

使い方:
    python -m scripts.backfill_sector_flow_features                 # 直近1年
    python -m scripts.backfill_sector_flow_features --days 60
    python -m scripts.backfill_sector_flow_features --from 2026-01-01 --to 2026-08-28
    python -m scripts.backfill_sector_flow_features --forward-only  # 将来リターンだけ再計算
    python -m scripts.backfill_sector_flow_features --dry-run

リクエスト数の目安:
    連続営業日を古い順に処理し前営業日の日足を使い回すため、1営業日あたり
    2リクエスト（日足＋銘柄一覧）＋ TOPIX を期間で1回。1年で約490。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

from loguru import logger

from src.analyzer.market_breadth import compute_topix_change
from src.analyzer.sector_flow_features import (
    compute_forward_returns,
    compute_sector_features,
)
from src.data_fetcher.jquants_api_client import (
    JQuantsApiClient,
    JQuantsError,
    JQuantsNotConfiguredError,
)
from src.storage.db import (
    get_saved_sector_feature_dates,
    get_sector_flow_features_df,
    update_sector_forward_returns,
    upsert_sector_flow_features,
)

DEFAULT_DAYS = 245  # 約1年ぶんの営業日


def _resolve_range(args: argparse.Namespace) -> tuple[str, str]:
    end = datetime.strptime(args.to, "%Y-%m-%d") if args.to else datetime.now()
    if args.from_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d")
    else:
        start = end - timedelta(days=int(args.days * 1.6) + 10)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _business_days(client: JQuantsApiClient, from_date: str, to_date: str) -> list[str]:
    rows = client.get_trading_calendar(from_date, to_date)
    return sorted(
        row["Date"] for row in rows
        if row.get("Date") and str(row.get("HolDiv")) == "1"
    )


def _refresh_forward_returns() -> int:
    """保存済みの全期間から将来リターンを計算し直して更新する。

    毎回全期間を計算し直すのは、直近の行が「先の営業日が足りず None」の状態で
    保存されており、日が進むたびに埋められるようになるため。
    冪等なので何度実行しても結果は同じ。
    """
    stored = get_sector_flow_features_df()
    if stored.empty:
        logger.info("保存済みの特徴量が無いため将来リターンの更新をスキップします")
        return 0

    rows = stored[["date", "s33_code", "ret_cap_weighted", "excess_ret_vs_topix"]]
    values = compute_forward_returns(rows.to_dict("records"))
    return update_sector_forward_returns(values)


def run(args: argparse.Namespace) -> int:
    backend = "Supabase(PostgreSQL)" if os.environ.get("DATABASE_URL") else "ローカルSQLite"

    if args.forward_only:
        logger.info(f"将来リターンのみ更新 | DB={backend}")
        updated = _refresh_forward_returns()
        logger.success(f"将来リターン更新完了: {updated}件")
        return 0

    client = JQuantsApiClient()
    if not client.is_configured:
        raise JQuantsNotConfiguredError("JQUANTS_API_KEY が未設定です。")

    from_date, to_date = _resolve_range(args)
    logger.info(f"特徴量バックフィル開始 | DB={backend} | 期間 {from_date} 〜 {to_date}")

    business_days = _business_days(client, from_date, to_date)
    if not args.from_date:
        business_days = business_days[-(args.days + 1):]
    if len(business_days) < 2:
        raise RuntimeError(f"営業日が足りません（{len(business_days)}日）。")

    target_days = business_days[1:]
    logger.info(f"対象営業日: {len(target_days)}日（{target_days[0]} 〜 {target_days[-1]}）")

    already_saved = set() if args.force else set(get_saved_sector_feature_dates())
    if already_saved:
        logger.info(f"保存済みをスキップします（{len(already_saved)}日ぶん保存済み）")

    topix_bars = client.get_topix_bars(business_days[0], business_days[-1])

    prev_date = business_days[0]
    prev_bars = None
    saved_total = 0
    skipped = 0
    failed: list[str] = []

    for index, target_date in enumerate(target_days, start=1):
        if target_date in already_saved:
            skipped += 1
            prev_date, prev_bars = target_date, None
            continue

        try:
            if prev_bars is None:
                prev_bars = client.get_daily_bars(prev_date)

            bars_today = client.get_daily_bars(target_date)
            master = client.get_listed_master(target_date)
            topix = compute_topix_change(topix_bars, target_date)

            features = compute_sector_features(
                target_date, bars_today, prev_bars, master,
                topix_change_pct=topix.change_pct if topix else None,
            )

            if args.dry_run:
                sample = next((f for f in features if f.s33_code == "3650"), None)
                logger.info(
                    f"[{index}/{len(target_days)}] {target_date} (dry-run) 業種数={len(features)}"
                    + (
                        f" | 電気機器 騰落{sample.ret_cap_weighted}% "
                        f"VWAP超{sample.above_vwap_pct}% 高値引け{sample.high_close_pct}%"
                        if sample else ""
                    )
                )
            else:
                saved_total += upsert_sector_flow_features([f.to_dict() for f in features])
                if index % 20 == 0 or index == len(target_days):
                    logger.info(f"[{index}/{len(target_days)}] {target_date} まで保存")

        except JQuantsError as exc:
            logger.warning(f"{target_date} の取得に失敗: {exc}")
            failed.append(target_date)
            bars_today = None

        prev_date = target_date
        prev_bars = bars_today

    updated = 0
    if not args.dry_run:
        logger.info("将来リターンを計算して更新します")
        updated = _refresh_forward_returns()

    logger.success(
        f"特徴量バックフィル完了 | 保存 {saved_total}件 / 将来リターン {updated}件 / "
        f"スキップ {skipped}日 / 失敗 {len(failed)}日"
    )
    if failed:
        logger.warning(f"失敗した営業日: {failed}（再実行で冪等に埋まります）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="業種別フロー特徴量のバックフィル（Phase 0・判定は行わない）"
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"--from 未指定時に遡る営業日数（既定: {DEFAULT_DAYS}）")
    parser.add_argument("--from", dest="from_date", help="開始日 YYYY-MM-DD")
    parser.add_argument("--to", help="終了日 YYYY-MM-DD（既定: 本日）")
    parser.add_argument("--force", action="store_true", help="保存済みの日付も取り直す")
    parser.add_argument("--dry-run", action="store_true", help="DBに書かず算出だけ行う")
    parser.add_argument("--forward-only", action="store_true",
                        help="取得を行わず、保存済みデータから将来リターンだけ再計算する")
    args = parser.parse_args()

    try:
        sys.exit(run(args))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"バックフィル失敗: {exc}")
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
