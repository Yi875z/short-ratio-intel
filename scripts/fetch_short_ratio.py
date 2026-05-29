"""
scripts/fetch_short_ratio.py

GitHub Actions / コマンドラインから実行するスタンドアロンの定時パイプライン。
Streamlit UI を介さず、以下を一気通貫で実行する:

    1. 空売り比率データ取得（JPX公式PDF + stock-marketdata フォールバック） → DB
    2. 市場テーマ判定（Tavilyニュース取り込み）                              → DB
    3. Gemini AIレポート生成（ニュース増補あり）                            → DB
    4. 実行サマリーを Slack へ通知（SLACK_WEBHOOK_URL 設定時のみ）

接続先 DB は環境変数 DATABASE_URL で決まる:
    - DATABASE_URL 設定あり → Supabase(PostgreSQL)   ← GitHub Actions / Streamlit Cloud
    - DATABASE_URL 未設定   → ローカル SQLite          ← 開発用（従来どおり）

使い方:
    python -m scripts.fetch_short_ratio                  # 直近5営業日を取得し最新日でフル処理
    python -m scripts.fetch_short_ratio --days 10        # 取得対象営業日数を変更
    python -m scripts.fetch_short_ratio --date 2026-05-28  # 特定日だけ取得・処理
    python -m scripts.fetch_short_ratio --no-theme       # 市場テーマ判定をスキップ
    python -m scripts.fetch_short_ratio --no-report      # AIレポート生成をスキップ
    python -m scripts.fetch_short_ratio --no-news        # Tavilyニュース取り込みを無効化（オフライン安全）

Streamlit には依存しない（app.streamlit_app から取り込むのは UI を含まない純粋な取得関数のみ）。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

# `python scripts/fetch_short_ratio.py` 直叩きでも import が通るようプロジェクトルートを通す
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

# ローカル実行用に .env を読む（GitHub Actions では env から渡るので no-op）
load_dotenv(_PROJECT_ROOT / ".env")

import requests
from loguru import logger

from config.settings import GEMINI_MODEL, SLACK_WEBHOOK_URL
from src.ai_engine.gemini_client import GeminiReportGenerator
from src.analyzer.anomaly_detector import AnomalyDetector
from src.analyzer.ratio_calculator import RatioCalculator
from src.macro_context.context_builder import (
    build_market_context_bundle,
    build_theme_snapshot_dicts,
)
from src.storage.db import (
    get_latest_date,
    get_market_short_ratio_df,
    save_ai_report,
    save_market_news_snapshots,
    save_market_theme_snapshots,
)

# UI を含まない純粋な取得ロジックだけを Streamlit アプリから再利用（DRY）。
# app.streamlit_app の main() は __main__ ガード下にあるため import しても起動しない。
from app.streamlit_app import (
    fetch_and_store_recent_short_ratio,
    fetch_and_store_short_ratio_date,
)

DEFAULT_DAYS = 5


# ──────────────────────────────────────────────────────────────
# 各ステップ
# ──────────────────────────────────────────────────────────────
def _step_fetch(target_date: str | None, days: int) -> dict:
    """ステップ1: 空売り比率データを取得して DB に保存。"""
    if target_date:
        logger.info(f"[1/3] 指定日を取得: {target_date}")
        result = fetch_and_store_short_ratio_date(target_date)
    else:
        logger.info(f"[1/3] 直近{days}営業日を取得")
        result = fetch_and_store_recent_short_ratio(days)
    logger.info(
        "取得完了: sector={saved_sector} market={saved_market} ({target_date})".format(
            saved_sector=result.get("saved_sector"),
            saved_market=result.get("saved_market"),
            target_date=result.get("target_date"),
        )
    )
    return result


def _prepare_analysis(report_date: str):
    """レポート/テーマ判定に必要なデータ束を組み立てる（main() と同じ手順）。"""
    calc = RatioCalculator()
    today_summary = calc.get_today_summary(report_date)
    weekly_df = calc.get_weekly_trend(report_date, days=14)
    anomalies = AnomalyDetector().detect(today_summary, weekly_df)
    market_trend_df = get_market_short_ratio_df(to_date=report_date)
    return calc, today_summary, weekly_df, anomalies, market_trend_df


def _step_theme(report_date: str, today_summary: dict, auto_fetch_news: bool) -> int:
    """ステップ2: 市場テーマ判定（Tavilyニュース取り込み）→ DB 保存。保存テーマ件数を返す。"""
    logger.info(f"[2/3] 市場テーマ判定 (auto_fetch_news={auto_fetch_news})")
    bundle = build_market_context_bundle(
        target_date=report_date,
        today_summary=today_summary,
        manual_news="",
        auto_fetch_news=auto_fetch_news,
    )
    theme_dicts = build_theme_snapshot_dicts(
        report_date,
        today_summary,
        manual_news=bundle.combined_news_text,
    )
    save_market_theme_snapshots(report_date, theme_dicts)
    save_market_news_snapshots(
        report_date,
        [item.to_dict() for item in bundle.fetched_news],
    )
    logger.info(
        f"テーマ保存: {len(theme_dicts)}件 / 取得ニュース: {len(bundle.fetched_news)}件"
    )
    return len(theme_dicts)


def _step_report(
    report_date: str,
    today_summary: dict,
    weekly_df,
    anomalies: list,
    auto_fetch_news: bool,
) -> int:
    """ステップ3: Gemini AIレポート生成 → DB 保存。生成文字数を返す。"""
    logger.info(f"[3/3] Gemini AIレポート生成 (model={GEMINI_MODEL})")
    generator = GeminiReportGenerator()
    report_obj, markdown = generator.generate_report(
        report_date,
        today_summary,
        weekly_df,
        anomalies,
        auto_fetch_news=auto_fetch_news,
    )
    save_ai_report(
        report_date,
        report_obj.current_macro_context,
        markdown,
        report_json=report_obj.model_dump_json(ensure_ascii=False),
        model_used=GEMINI_MODEL,
    )
    logger.info(f"AIレポート保存完了: {len(markdown)}文字")
    return len(markdown)


def _notify_slack(text: str) -> None:
    """SLACK_WEBHOOK_URL が設定されていればサマリーを通知（失敗しても本処理は止めない）。"""
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL 未設定のため通知をスキップ")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
        resp.raise_for_status()
        logger.info("Slack 通知を送信しました")
    except Exception as exc:  # noqa: BLE001 通知失敗で本処理を落とさない
        logger.warning(f"Slack 通知に失敗: {exc}")


# ──────────────────────────────────────────────────────────────
# エントリポイント
# ──────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    backend = "Supabase(PostgreSQL)" if os.environ.get("DATABASE_URL") else "ローカルSQLite"
    logger.info(f"パイプライン開始 | DB={backend}")

    fetch_result = _step_fetch(args.date, args.days)

    # 処理対象日: --date 指定があればそれ、なければ DB 最新日
    report_date = args.date or get_latest_date()
    if not report_date:
        raise RuntimeError("DB に保存済みデータがなく、対象日を決定できません")

    theme_count = 0
    report_chars = 0

    if args.no_report and args.no_theme:
        logger.info("レポート・テーマともにスキップ指定。取得のみで終了します。")
    else:
        _, today_summary, weekly_df, anomalies, _ = _prepare_analysis(report_date)
        auto_fetch_news = not args.no_news

        if not args.no_theme:
            theme_count = _step_theme(report_date, today_summary, auto_fetch_news)

        if not args.no_report:
            report_chars = _step_report(
                report_date, today_summary, weekly_df, anomalies, auto_fetch_news
            )

    summary = (
        f"✅ 空売り比率パイプライン完了 ({report_date})\n"
        f"・取得: sector={fetch_result.get('saved_sector')} / "
        f"market={fetch_result.get('saved_market')}\n"
        f"・市場テーマ: {theme_count}件\n"
        f"・AIレポート: {report_chars}文字 ({GEMINI_MODEL})\n"
        f"・DB: {backend}"
    )
    logger.success(summary)
    _notify_slack(summary)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="空売り比率インテリジェンス 定時取得パイプライン（GitHub Actions / CLI 用）"
    )
    parser.add_argument(
        "--date",
        help="特定日のみ取得・処理する YYYY-MM-DD。未指定なら直近営業日をまとめて取得。",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"--date 未指定時に取得する直近営業日数（既定: {DEFAULT_DAYS}）",
    )
    parser.add_argument("--no-theme", action="store_true", help="市場テーマ判定をスキップ")
    parser.add_argument("--no-report", action="store_true", help="AIレポート生成をスキップ")
    parser.add_argument(
        "--no-news",
        action="store_true",
        help="Tavilyニュース取り込みを無効化（テーマ/レポートは内部データのみで作成）",
    )
    args = parser.parse_args()

    try:
        sys.exit(run(args))
    except Exception as exc:  # noqa: BLE001 失敗をログ+Slackに残して非ゼロ終了
        logger.error(f"パイプライン失敗: {exc}")
        logger.error(traceback.format_exc())
        _notify_slack(f"❌ 空売り比率パイプライン失敗: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
