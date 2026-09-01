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
from src.analyzer.market_breadth import (
    compute_all_breadth,
    compute_topix_change,
    previous_business_day,
)
from src.analyzer.ratio_calculator import RatioCalculator
from src.data_fetcher.jquants_api_client import JQuantsApiClient, JQuantsError
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
    upsert_market_breadth_records,
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
    # 取得0件でも後段（テーマ判定・レポート生成）はDBの既存データで走り切ってしまうため、
    # そのままだとワークフローが success で終わり欠測が無通知になる（2026-08 に3営業日欠測）。
    # 取得元の表記変更を当日中に気づけるよう、ここで明示的に落とす。
    if not result.get("saved_sector"):
        raise RuntimeError(
            "空売り比率の業種別データを1件も取得できませんでした "
            f"(market={result.get('saved_market')})。"
            "stock-marketdata.com と JPX公表PDF の双方が取得不能か、"
            "公開元の表記変更が疑われます。"
        )
    return result


def _step_breadth(report_date: str) -> dict:
    """騰落銘柄数・TOPIX騰落率を J-Quants から取得して保存する。

    ⚠️ ここは **fail-soft**。空売り比率（_step_fetch）と違って、失敗しても
    パイプラインを止めない。理由は3つ:
      - 空売り比率はこのアプリの本体だが、騰落銘柄数は文脈情報である
      - J-Quants は契約プランに依存する外部依存であり、ここで全体を落とすと
        AIレポートまで巻き添えになる
      - 需給モニターは欠損を「未取得」として明示し、それを必要とするレジームを
        判定しない設計になっている（0で埋めた誤判定は起きない）

    Returns:
        {"saved": 保存件数, "error": エラー文字列 or None}
    """
    from datetime import datetime, timedelta

    logger.info(f"[追加] 騰落銘柄数・TOPIX騰落率を取得: {report_date}")
    try:
        client = JQuantsApiClient()
        if not client.is_configured:
            logger.warning(
                "JQUANTS_API_KEY が未設定のため騰落銘柄数はスキップします"
                "（需給モニターでは『未取得』と表示されます）"
            )
            return {"saved": 0, "error": "APIキー未設定"}

        # 前営業日は公式の取引カレンダーで決める（日付の引き算で1日ずれない）
        start = (datetime.strptime(report_date, "%Y-%m-%d") - timedelta(days=14))
        calendar_rows = client.get_trading_calendar(
            start.strftime("%Y-%m-%d"), report_date
        )
        prev_date = previous_business_day(calendar_rows, report_date)
        if not prev_date:
            return {"saved": 0, "error": "前営業日を特定できません"}

        bars_today = client.get_daily_bars(report_date)
        bars_prev = client.get_daily_bars(prev_date)
        master = client.get_listed_master(report_date)
        breadth = compute_all_breadth(report_date, bars_today, bars_prev, master)

        topix = compute_topix_change(
            client.get_topix_bars(prev_date, report_date), report_date
        )

        records = []
        for counts in breadth.values():
            record = counts.to_dict()
            if topix is not None:
                record["topix_close"] = topix.close
                record["topix_prev_close"] = topix.prev_close
                record["topix_change_pct"] = topix.change_pct
            records.append(record)

        saved = upsert_market_breadth_records(records)
        prime = breadth.get("TSE_PRIME")
        if prime:
            logger.info(
                f"騰落銘柄数 保存 {saved}件 | プライム 値上がり{prime.advancing} "
                f"値下がり{prime.declining} | TOPIX "
                f"{topix.change_pct if topix else 'N/A'}%"
            )
        return {"saved": saved, "error": None}

    except JQuantsError as exc:
        logger.warning(f"騰落銘柄数の取得に失敗しました（処理は継続）: {exc}")
        return {"saved": 0, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 文脈情報の失敗で本処理を落とさない
        logger.warning(f"騰落銘柄数の処理で想定外のエラー（処理は継続）: {exc}")
        return {"saved": 0, "error": str(exc)}


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
):
    """ステップ3: Gemini AIレポート生成 → DB 保存。

    Returns:
        (生成文字数, ReadingReport オブジェクト, 実際に使われたモデル名) のタプル。
        通知に結論・レジームを載せるため report_obj も返す。
    """
    logger.info(f"[3/3] Gemini AIレポート生成 (model={GEMINI_MODEL})")
    generator = GeminiReportGenerator()
    report_obj, markdown = generator.generate_report(
        report_date,
        today_summary,
        weekly_df,
        anomalies,
        auto_fetch_news=auto_fetch_news,
    )
    # 日次クォータ枯渇時は generator が退避モデルへ自動で切り替わるため、
    # 設定値ではなく「実際に使われたモデル」を記録する。
    used_model = generator.model_name
    save_ai_report(
        report_date,
        report_obj.current_macro_context,
        markdown,
        report_json=report_obj.model_dump_json(),
        model_used=used_model,
    )
    logger.info(f"AIレポート保存完了: {len(markdown)}文字 (model={used_model})")
    return len(markdown), report_obj, used_model


def _format_report_highlights(report_obj) -> str:
    """AIレポートの結論を通知用の短いテキストに整形する。

    report_obj が None（--no-report 時）でも空文字を返して壊れない。
    毎日19時に自動生成されるレポートの要点を、アプリを開かずに通知だけで
    掴めるようにするための抜粋。
    """
    if report_obj is None:
        return ""

    lines: list[str] = []
    regime = (getattr(report_obj, "regime", "") or "").strip()
    if regime:
        lines.append(f"・レジーム: {regime}")

    summary = (getattr(report_obj, "executive_summary", "") or "").strip()
    if summary:
        lines.append("・結論:")
        for row in summary.splitlines():
            row = row.strip()
            if row:
                lines.append(f"    {row}")

    new_signal = (getattr(report_obj, "new_signal_summary", "") or "").strip()
    if new_signal and not new_signal.startswith("新規シグナルの専用分析は未生成"):
        head = [row.strip() for row in new_signal.splitlines() if row.strip()][:2]
        if head:
            lines.append("・新規シグナル: " + " / ".join(head))

    return "\n".join(lines)


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

    # 需給モニター用の文脈データ。失敗してもここで止めない（fail-soft）。
    breadth_result = _step_breadth(report_date)

    theme_count = 0
    report_chars = 0
    report_obj = None
    used_model = GEMINI_MODEL

    if args.no_report and args.no_theme:
        logger.info("レポート・テーマともにスキップ指定。取得のみで終了します。")
    else:
        _, today_summary, weekly_df, anomalies, _ = _prepare_analysis(report_date)
        auto_fetch_news = not args.no_news

        if not args.no_theme:
            theme_count = _step_theme(report_date, today_summary, auto_fetch_news)

        if not args.no_report:
            report_chars, report_obj, used_model = _step_report(
                report_date, today_summary, weekly_df, anomalies, auto_fetch_news
            )

    highlights = _format_report_highlights(report_obj)
    breadth_text = f"{breadth_result.get('saved')}件"
    if breadth_result.get("error"):
        breadth_text += f"（取得できず: {breadth_result['error']}）"

    summary = (
        f"✅ 空売り比率パイプライン完了 ({report_date})\n"
        f"・取得: sector={fetch_result.get('saved_sector')} / "
        f"market={fetch_result.get('saved_market')}\n"
        f"・騰落銘柄数: {breadth_text}\n"
        f"・市場テーマ: {theme_count}件\n"
        f"・AIレポート: {report_chars}文字 ({used_model})\n"
        f"・DB: {backend}"
    )
    if highlights:
        summary += "\n" + highlights
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
