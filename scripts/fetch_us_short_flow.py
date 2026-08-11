"""
scripts/fetch_us_short_flow.py

米国ショートフローの日次パイプライン（US-P2）。GitHub Actions / CLI から実行する。

    1. FINRA CNMS から直近営業日のショートボリュームを取得        → DB
    2. Yahoo Finance から日足OHLCVを取得                          → DB
    3. Zスコア・4象限分類・バスケット/ETF乖離を算出しレポート生成 → ファイル
    4. ハイライトを Slack へ通知（SLACK_WEBHOOK_URL 設定時のみ）

日本側の夕方パイプライン（scripts/fetch_short_ratio.py）とは完全に独立している。
本スクリプトは日本のテーブルに一切触れず、Gemini API も呼ばない。

実行時刻の考え方:
    FINRA の当日ファイルは米東部18:00頃に公開される。JST では夏時間で翌朝07:00、
    冬時間で08:00。遅延の余裕を見て JST 08:30 に実行する想定。
    未公開ならデータなしとして静かに終了する（fail-soft）。

使い方:
    python -m scripts.fetch_us_short_flow                  # 直近5暦日を取得し最新日でレポート
    python -m scripts.fetch_us_short_flow --date 2026-08-05  # 特定日だけ
    python -m scripts.fetch_us_short_flow --days 10        # 取得する暦日数を変更
    python -m scripts.fetch_us_short_flow --no-price       # 日足取得をスキップ
    python -m scripts.fetch_us_short_flow --no-slack       # Slack通知を抑止
    python -m scripts.fetch_us_short_flow --dry-run        # DB書き込みなし
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import requests
from loguru import logger

from config.settings import REPORTS_DIR, SLACK_WEBHOOK_URL
from config.us_universe import US_UNIVERSE
from src.data_fetcher.finra_client import FinraShortVolumeClient, normalize_date
from src.data_fetcher.finra_short_interest_client import FinraShortInterestClient
from src.data_fetcher.us_price_client import UsPriceClient
from src.report.us_daily_report import build_daily_report
from src.storage.db import (
    get_us_market_daily_df,
    get_us_short_interest_df,
    get_us_short_interest_latest_date,
    get_us_short_volume_df,
    upsert_us_market_daily_records,
    upsert_us_short_interest_records,
    upsert_us_short_volume_records,
)

DEFAULT_DAYS = 5


def _step_fetch_short_volume(args: argparse.Namespace) -> tuple[int, str | None]:
    """ステップ1: FINRA からショートボリュームを取得して保存する。

    Returns: (保存件数, 取得できた最新日) — データが無ければ (0, None)
    """
    client = FinraShortVolumeClient()

    if args.date:
        target = normalize_date(args.date)
        logger.info(f"[1/4] FINRA 取得（指定日）: {target}")
        records = client.get_daily_records(target, tickers=US_UNIVERSE)
    else:
        end = date.today()
        start = end - timedelta(days=args.days)
        logger.info(f"[1/4] FINRA 取得（直近{args.days}暦日）: {start} → {end}")
        records = client.get_range_records(start.isoformat(), end.isoformat(), tickers=US_UNIVERSE)

    if not records:
        logger.warning("FINRA からデータを取得できませんでした（未公開または休場）")
        return 0, None

    latest = max(r["Date"] for r in records)

    if args.dry_run:
        logger.info(f"[dry-run] 保存をスキップ: {len(records)}レコード / 最新 {latest}")
        return 0, latest

    return upsert_us_short_volume_records(records), latest


def _step_fetch_prices(args: argparse.Namespace, latest_date: str) -> int:
    """ステップ2: 日足OHLCVを取得して保存する。失敗しても止めない。"""
    if args.no_price:
        logger.info("[2/4] 日足取得をスキップ")
        return 0

    end = date.fromisoformat(latest_date)
    start = end - timedelta(days=max(args.days, 5))
    logger.info(f"[2/4] 日足取得: {start} → {end}")

    try:
        records = UsPriceClient().get_daily_ohlcv_bulk(US_UNIVERSE, start.isoformat(), end.isoformat())
    except Exception as e:  # noqa: BLE001 価格が取れなくてもフロー分析は続行する
        logger.warning(f"日足取得で例外。価格文脈なしで続行します: {e}")
        return 0

    if not records or args.dry_run:
        return 0
    return upsert_us_market_daily_records(records)


def _step_short_interest(args: argparse.Namespace) -> int:
    """ステップ3: 空売り残高（隔週）を取り込む。

    残高は月2回しか更新されないため、毎回の実行では基準日を確認するだけで済む。
    未取得の基準日が出たときだけ本体を引く（無駄な通信をしない）。
    """
    if args.no_short_interest:
        logger.info("[3/4] 空売り残高の取得をスキップ")
        return 0

    client = FinraShortInterestClient()
    latest = client.get_latest_settlement_date()
    if not latest:
        logger.warning("空売り残高の基準日を特定できませんでした")
        return 0

    stored = get_us_short_interest_latest_date()
    if stored == latest:
        logger.info(f"[3/4] 空売り残高は取得済み（基準日 {stored}）")
        return 0

    logger.info(f"[3/4] 空売り残高を取得: 基準日 {latest}（保存済みは {stored or 'なし'}）")
    records = client.get_short_interest(latest, tickers=US_UNIVERSE)
    if not records or args.dry_run:
        return 0
    return upsert_us_short_interest_records(records)


def _step_report(target_date: str) -> dict:
    """ステップ4: DBから読み直してレポートを生成する。"""
    logger.info(f"[4/4] レポート生成: {target_date}")
    short_df = get_us_short_volume_df()
    price_df = get_us_market_daily_df()
    si_df = get_us_short_interest_df(tickers=US_UNIVERSE, latest_only=True)
    return build_daily_report(target_date, short_df, price_df, short_interest_df=si_df)


def _save_report(report: dict) -> Path | None:
    """レポートを Markdown で保存する。失敗してもパイプラインは止めない。"""
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"{report['date']}_us_short_flow.md"
        path.write_text(report["markdown"], encoding="utf-8")
        logger.info(f"レポートを保存: {path}")
        return path
    except OSError as e:
        logger.warning(f"レポート保存に失敗: {e}")
        return None


def _notify_slack(text: str) -> None:
    """Slack へ通知する。未設定・失敗でもパイプラインは止めない。"""
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL 未設定のため通知をスキップ")
        return
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
        if response.status_code >= 300:
            logger.warning(f"Slack通知が失敗: HTTP {response.status_code}")
    except requests.RequestException as e:
        logger.warning(f"Slack通知に失敗: {e}")


def run(args: argparse.Namespace) -> int:
    backend = "Supabase(PostgreSQL)" if os.environ.get("DATABASE_URL") else "ローカルSQLite"
    logger.info(f"米国ショートフロー パイプライン開始 | DB={backend}")

    saved_short, latest_date = _step_fetch_short_volume(args)

    if not latest_date:
        # 米国休場・未公開。異常ではないので正常終了させる（Actionsを赤くしない）
        message = "🇺🇸 米国ショートフロー: 対象日のデータなし（未公開または休場）"
        logger.info(message)
        if not args.no_slack:
            _notify_slack(message)
        return 0

    saved_price = _step_fetch_prices(args, latest_date)
    saved_si = _step_short_interest(args)

    if args.dry_run:
        logger.info("[dry-run] DBへ未保存のためレポート生成をスキップ")
        return 0

    report = _step_report(latest_date)
    _save_report(report)

    summary = (
        f"✅ 米国ショートフロー完了 ({latest_date})\n"
        f"・取得: ショート{saved_short}件 / 日足{saved_price}件 / 残高{saved_si}件\n"
        f"・DB: {backend}\n"
        f"{report['highlights']}"
    )
    logger.success(summary)
    if not args.no_slack:
        _notify_slack(summary)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="米国ショートフロー 日次パイプライン（GitHub Actions / CLI 用）"
    )
    parser.add_argument("--date", help="特定日のみ取得・処理する YYYY-MM-DD")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"--date 未指定時にさかのぼる暦日数（既定: {DEFAULT_DAYS}）",
    )
    parser.add_argument("--no-price", action="store_true", help="日足OHLCVの取得をスキップ")
    parser.add_argument(
        "--no-short-interest", action="store_true",
        help="空売り残高（隔週）の取得をスキップ",
    )
    parser.add_argument("--no-slack", action="store_true", help="Slack通知を行わない")
    parser.add_argument("--dry-run", action="store_true", help="DBへ書き込まない")
    parser.add_argument("--verbose", action="store_true", help="DEBUGログを出力")
    args = parser.parse_args()

    if args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")

    try:
        sys.exit(run(args))
    except Exception as exc:  # noqa: BLE001 失敗をログ+Slackに残して非ゼロ終了
        logger.error(f"米国パイプライン失敗: {exc}")
        logger.error(traceback.format_exc())
        if not args.no_slack:
            _notify_slack(f"❌ 米国ショートフロー失敗: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
