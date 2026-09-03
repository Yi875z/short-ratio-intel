"""
scripts/backfill_jpx_breakdown.py

JPX内訳（実注文・価格規制あり・なし・空売り代金）が 0 で保存されている過去日を、
JPXの月別アーカイブから取り直してDBへ書き戻す。

なぜ必要か:
    2026-04-20〜08-31 の22営業日ぶんの内訳が 0 で上書きされていた。
    内訳を持たない stock-marketdata スクレイパーの結果で既存行を潰したためで、
    上書き自体は db 側の保護で止めたが、既に潰れた行は取り直すしかない。

    実測（2026-09-03）: 潰れていた22日は5つの塊で、すべてその月の最終営業日で
    終わっていた（4/30・5/29・6/30・7/31・8/31）。一覧ページは当月ぶんしか
    載せないため、前月以前の日付は一覧から引けない。
    推定（実行ログが無いため未確定）: 月替わり後の日次実行が直近N営業日を
    取りに行き、前月末ぶんがスクレイパーへ落ちて 0 で上書きした。

取得元:
    一覧ページが当月ぶんの全営業日、月別アーカイブ 00-archives-01〜12.html が
    過去12ヶ月ぶんの全営業日（01が前月、12が13ヶ月前）。
    2026-09-03 実測でページの日付集合とDBの営業日が完全一致することを確認済み。
    したがって直近13ヶ月以内の欠測はすべて取り直せる。

使い方:
    python -m scripts.backfill_jpx_breakdown                 # ドライラン（DBに書かない）
    python -m scripts.backfill_jpx_breakdown --apply         # 実際に書き戻す
    python -m scripts.backfill_jpx_breakdown --from 2026-07-01 --to 2026-08-31 --apply
    python -m scripts.backfill_jpx_breakdown --market-only   # 東証全体だけ

既定はドライランである。本番DBを黙って書き換えないため、--apply を明示すること。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

load_dotenv()

from src.data_fetcher.jpx_pdf_client import JPXShortSellingClient  # noqa: E402
from src.storage.db import (  # noqa: E402
    delete_short_ratio_records_for_dates,
    get_db_engine,
    upsert_market_short_ratio_records,
    upsert_short_ratio_records,
)

# 内訳3列がすべて0の行を「未取得」とみなす。東証全体・業種別いずれも
# 空売り代金が真に0の営業日は存在しないため、この判定で取りこぼしは出ない。
_MARKET_GAP_SQL = """
SELECT date FROM market_short_ratio_daily
WHERE COALESCE(total_short_va, 0) = 0
  AND COALESCE(shrt_with_res_va, 0) = 0
  AND COALESCE(shrt_no_res_va, 0) = 0
  AND date BETWEEN :from_date AND :to_date
ORDER BY date
"""

_SECTOR_GAP_SQL = """
SELECT date FROM short_ratio_daily
WHERE date BETWEEN :from_date AND :to_date
GROUP BY date
HAVING SUM(COALESCE(total_short_va, 0)
         + COALESCE(shrt_with_res_va, 0)
         + COALESCE(shrt_no_res_va, 0)) = 0
ORDER BY date
"""


def find_gap_dates(sql: str, from_date: str, to_date: str) -> list[str]:
    engine = get_db_engine()
    with engine.connect() as connection:
        rows = connection.execute(
            text(sql), {"from_date": from_date, "to_date": to_date}
        ).fetchall()
    return [r[0] for r in rows]


def backfill(
    from_date: str,
    to_date: str,
    apply_changes: bool,
    market: bool = True,
    sector: bool = True,
) -> dict:
    client = JPXShortSellingClient()
    summary = {
        "market_target": [], "market_recovered": [], "market_unavailable": [],
        "sector_target": [], "sector_recovered": [], "sector_unavailable": [],
    }

    if market:
        summary["market_target"] = find_gap_dates(_MARKET_GAP_SQL, from_date, to_date)
        for target_date in summary["market_target"]:
            record = client.get_market_breakdown_by_date(target_date)
            if not record or not record.get("TotalShortVa"):
                summary["market_unavailable"].append(target_date)
                logger.warning(f"{target_date}: 東証全体の内訳をアーカイブから取得できません")
                continue
            if apply_changes:
                upsert_market_short_ratio_records([record])
            summary["market_recovered"].append(target_date)
            logger.info(
                f"{target_date}: 東証全体 空売り代金 {record['TotalShortVa']:,.0f}百万円"
                f" / 比率 {record['ShortRatioPct']:.2f}%"
                + ("" if apply_changes else "（ドライラン・未保存）")
            )

    if sector:
        summary["sector_target"] = find_gap_dates(_SECTOR_GAP_SQL, from_date, to_date)
        for target_date in summary["sector_target"]:
            records = client.get_sector_breakdown_by_date(target_date)
            if not records:
                summary["sector_unavailable"].append(target_date)
                logger.warning(f"{target_date}: 業種別の内訳をアーカイブから取得できません")
                continue
            if apply_changes:
                # 内訳を持つ結果なので入れ替えてよい（db.dates_with_breakdown と同じ条件）。
                delete_short_ratio_records_for_dates([target_date])
                upsert_short_ratio_records(records)
            summary["sector_recovered"].append(target_date)
            logger.info(
                f"{target_date}: 業種別 {len(records)}件"
                + ("" if apply_changes else "（ドライラン・未保存）")
            )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="JPX内訳の欠測をアーカイブから復旧する")
    parser.add_argument("--from", dest="from_date", default="2000-01-01")
    parser.add_argument("--to", dest="to_date", default="2999-12-31")
    parser.add_argument("--apply", action="store_true", help="実際にDBへ書き戻す（既定はドライラン）")
    parser.add_argument("--market-only", action="store_true")
    parser.add_argument("--sector-only", action="store_true")
    args = parser.parse_args()

    mode = "本番書き込み" if args.apply else "ドライラン（DBに書きません）"
    logger.info(f"JPX内訳の復旧を開始します: {args.from_date} 〜 {args.to_date} / {mode}")

    summary = backfill(
        args.from_date,
        args.to_date,
        apply_changes=args.apply,
        market=not args.sector_only,
        sector=not args.market_only,
    )

    logger.info(
        "東証全体: 対象{}日 / 復旧{}日 / 取得不可{}日".format(
            len(summary["market_target"]),
            len(summary["market_recovered"]),
            len(summary["market_unavailable"]),
        )
    )
    logger.info(
        "業種別: 対象{}日 / 復旧{}日 / 取得不可{}日".format(
            len(summary["sector_target"]),
            len(summary["sector_recovered"]),
            len(summary["sector_unavailable"]),
        )
    )
    if summary["market_unavailable"] or summary["sector_unavailable"]:
        logger.warning(
            "取得不可の日はアーカイブの保持期間（過去12ヶ月＋当月）を過ぎています。"
            "この範囲を外れた日は取り直せません。"
        )
    if not args.apply:
        logger.info("ドライランのため何も保存していません。書き戻すには --apply を付けてください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
