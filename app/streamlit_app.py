"""
short-ratio-intel Streamlit app.

通常のPythonソースとして保守できるアプリ本体。旧復旧用の
app/__pycache__/streamlit_app_original.cpython-311.pyc は削除せず残す。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import GEMINI_MODEL, MARKET_NEWS_AUTO_FETCH, TAVILY_API_KEY
from src.ai_engine.gemini_client import GeminiReportGenerator
from src.ai_engine.prompt_builder import build_theme_transition_context_for_prompt
from src.ai_engine.report_quality import (
    build_quality_feedback_prompt_block,
    evaluate_report_quality,
)
from src.analyzer.anomaly_detector import AnomalyDetector
from src.analyzer.flow_signal_analyzer import FlowSignalAnalyzer
from src.analyzer.ratio_calculator import RatioCalculator
from src.data_fetcher.jpx_pdf_client import JPXShortSellingClient
from src.data_fetcher.jquants_client import JQuantsClient
from src.macro_context.context_builder import (
    build_market_context_bundle,
    build_theme_snapshot_dicts,
)
from src.macro_context.theme_history import (
    build_theme_comparison_rows,
    build_theme_history_rows,
    find_previous_theme_date,
)
from src.storage.db import (
    delete_short_ratio_records_for_dates,
    get_ai_report,
    get_ai_report_dates,
    get_market_news_snapshots,
    get_market_short_ratio_df,
    get_market_theme_snapshot_dates,
    get_market_theme_snapshots,
    get_saved_short_ratio_dates,
    save_ai_report,
    save_market_news_snapshots,
    save_market_theme_snapshots,
    upsert_market_short_ratio_records,
    upsert_short_ratio_records,
)


AUTO_FETCH_DAYS = 5


def main() -> None:
    st.set_page_config(
        page_title="空売り比率インテリジェンス",
        layout="wide",
    )
    _apply_style()

    st.title("空売り比率インテリジェンス")
    st.caption("JPX日次空売りフロー、業種別内訳、市場テーマ、Gemini AIレポートを一画面で確認します。")

    selected_date = _sidebar()
    if not selected_date:
        st.info("DBに保存済みの日付がありません。左メニューからデータ取得を実行してください。")
        return

    calc = RatioCalculator()
    today_summary = calc.get_today_summary(selected_date)
    if not today_summary:
        st.warning(f"{selected_date} のデータが見つかりません。")
        return

    weekly_df = calc.get_weekly_trend(selected_date, days=14)
    market_trend_df = get_market_short_ratio_df(to_date=selected_date)
    anomalies = AnomalyDetector().detect(today_summary, weekly_df)
    _attach_flow_signals(today_summary, selected_date, calc, market_trend_df)

    overview_tab, sectors_tab, breakdown_tab, theme_tab, report_tab, history_tab = st.tabs(
        ["概要", "業種", "JPX内訳", "市場テーマ", "AIレポート", "履歴"]
    )

    with overview_tab:
        _render_overview(selected_date, today_summary, market_trend_df, anomalies)
    with sectors_tab:
        _render_sectors(today_summary, weekly_df)
    with breakdown_tab:
        _render_breakdown(today_summary)
    with theme_tab:
        _render_market_theme_tab(selected_date, today_summary)
    with report_tab:
        _render_ai_report_tab(selected_date, today_summary, weekly_df, anomalies)
    with history_tab:
        _render_history_tab(selected_date)


def _sidebar() -> str | None:
    with st.sidebar:
        st.header("操作")
        dates = get_saved_short_ratio_dates()
        selected_date = dates[0] if dates else None

        if dates:
            selected_date = st.selectbox("分析日", dates, index=0)

        manual_date = st.date_input("取得日", value=date.today())
        if st.button("指定日を取得", use_container_width=True):
            target = manual_date.strftime("%Y-%m-%d")
            with st.spinner(f"{target} の空売りデータを取得中..."):
                result = fetch_and_store_short_ratio_date(target)
            _show_fetch_result(result)
            st.rerun()

        if st.button(f"直近{AUTO_FETCH_DAYS}営業日を取得", use_container_width=True):
            with st.spinner("直近データを取得中..."):
                result = fetch_and_store_recent_short_ratio(AUTO_FETCH_DAYS)
            _show_fetch_result(result)
            st.rerun()

        st.divider()
        st.caption("自動ニュース取得")
        st.write(f"既定: {'ON' if MARKET_NEWS_AUTO_FETCH else 'OFF'}")
        st.write(f"Tavily API: {'設定済み' if TAVILY_API_KEY else '未設定'}")

    return selected_date


def fetch_and_store_short_ratio_date(target_date: str) -> dict:
    """指定日のstock-marketdataとJPX公式PDFを取得して保存する。"""
    scraper = JQuantsClient()
    jpx = JPXShortSellingClient()

    sector_records = jpx.get_sector_breakdown_by_date(target_date)
    sector_source = "jpx_pdf"
    if not sector_records:
        sector_records = scraper.get_short_ratio_by_date(target_date)
        sector_source = "stock-marketdata"

    market_record = jpx.get_market_breakdown_by_date(target_date)
    market_source = "jpx_pdf"
    if not market_record:
        market_record = scraper.get_market_short_ratio_by_date(target_date)
        market_source = "stock-marketdata"

    saved_sector = 0
    saved_market = 0
    if sector_records:
        delete_short_ratio_records_for_dates([target_date])
        saved_sector = upsert_short_ratio_records(sector_records)
    if market_record:
        saved_market = upsert_market_short_ratio_records([market_record])

    return {
        "target_date": target_date,
        "saved_sector": saved_sector,
        "saved_market": saved_market,
        "sector_source": sector_source if sector_records else "none",
        "market_source": market_source if market_record else "none",
    }


def fetch_and_store_recent_short_ratio(days: int = AUTO_FETCH_DAYS) -> dict:
    """直近N営業日を取得し、可能な日はJPX公式PDFの内訳で補完する。"""
    scraper = JQuantsClient()
    jpx = JPXShortSellingClient()

    fallback_sector_records = scraper.get_recent_days(days)
    fallback_market_records = scraper.get_market_recent_days(days)
    candidate_dates = sorted(
        {record["Date"] for record in fallback_sector_records},
        reverse=True,
    )[:days]

    all_sector_records = []
    all_market_records = []
    source_by_date = {}
    for current_date in candidate_dates:
        sector_records = jpx.get_sector_breakdown_by_date(current_date)
        sector_source = "jpx_pdf"
        if not sector_records:
            sector_records = [
                record
                for record in fallback_sector_records
                if record["Date"] == current_date
            ]
            sector_source = "stock-marketdata"
        if sector_records:
            all_sector_records.extend(sector_records)

        market_record = jpx.get_market_breakdown_by_date(current_date)
        market_source = "jpx_pdf"
        if not market_record:
            market_record = next(
                (
                    record
                    for record in fallback_market_records
                    if record["Date"] == current_date
                ),
                None,
            )
            market_source = "stock-marketdata"
        if market_record:
            all_market_records.append(market_record)

        source_by_date[current_date] = {
            "sector": sector_source if sector_records else "none",
            "market": market_source if market_record else "none",
        }

    if candidate_dates:
        delete_short_ratio_records_for_dates(candidate_dates)

    return {
        "target_date": ", ".join(candidate_dates),
        "saved_sector": upsert_short_ratio_records(all_sector_records),
        "saved_market": upsert_market_short_ratio_records(all_market_records),
        "sector_source": "mixed",
        "market_source": "mixed",
        "source_by_date": source_by_date,
    }


def _attach_flow_signals(
    today_summary: dict,
    selected_date: str,
    calc: RatioCalculator,
    market_trend_df: pd.DataFrame,
) -> None:
    analyzer = FlowSignalAnalyzer()
    today_summary["flow_signals"] = analyzer.detect(today_summary, market_trend_df)

    dates = [
        current_date
        for current_date in sorted(get_saved_short_ratio_dates())
        if current_date <= selected_date
    ][-14:]
    summaries = {current_date: calc.get_today_summary(current_date) for current_date in dates}
    history = analyzer.build_history(dates, summaries, market_trend_df)
    today_summary["flow_signal_history"] = history["rows"]


def _render_overview(
    selected_date: str,
    today_summary: dict,
    market_trend_df: pd.DataFrame,
    anomalies: list,
) -> None:
    market_ratio = today_summary.get("market_ratio")
    market_dod = today_summary.get("market_dod_change")
    cols = st.columns(4)
    cols[0].metric("東証全体", _pct(market_ratio), _pt(market_dod))
    cols[1].metric("業種数", today_summary.get("sector_count", 0))
    cols[2].metric("シグナル", len(today_summary.get("flow_signals", [])))
    cols[3].metric("異常値", len(anomalies))

    if not market_trend_df.empty:
        trend = market_trend_df.sort_values("date").tail(30)
        fig = px.line(
            trend,
            x="date",
            y="short_ratio_pct",
            markers=True,
            title="東証全体 空売り比率推移",
        )
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    high_col, low_col = st.columns(2)
    with high_col:
        st.subheader("高空売り 上位5業種")
        st.dataframe(_sector_frame(today_summary.get("top5_high", [])), hide_index=True)
    with low_col:
        st.subheader("低空売り 下位5業種")
        st.dataframe(_sector_frame(today_summary.get("top5_low", [])), hide_index=True)

    if anomalies:
        st.subheader("異常値")
        st.dataframe(pd.DataFrame([a.__dict__ for a in anomalies]), hide_index=True)


def _render_sectors(today_summary: dict, weekly_df: pd.DataFrame) -> None:
    sector_df = pd.DataFrame(today_summary.get("sector_data", []))
    if sector_df.empty:
        st.info("業種データがありません。")
        return

    fig = px.bar(
        sector_df.sort_values("short_ratio_pct", ascending=True),
        x="short_ratio_pct",
        y="sector_name",
        color="zone_label",
        orientation="h",
        title="業種別 空売り比率",
    )
    fig.update_layout(height=720, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)

    columns = [
        "sector_name", "short_ratio_pct", "dod_change",
        "shrt_with_res_va", "shrt_no_res_va", "total_volume_va", "zone_label",
    ]
    st.dataframe(
        sector_df[[col for col in columns if col in sector_df.columns]],
        hide_index=True,
        use_container_width=True,
    )

    if not weekly_df.empty:
        selected_sector = st.selectbox(
            "業種別推移",
            sorted(weekly_df["sector_name"].dropna().unique()),
        )
        trend = weekly_df[weekly_df["sector_name"] == selected_sector].sort_values("date")
        fig = px.line(
            trend,
            x="date",
            y="short_ratio_pct",
            markers=True,
            title=f"{selected_sector} 空売り比率推移",
        )
        st.plotly_chart(fig, use_container_width=True)


def _render_breakdown(today_summary: dict) -> None:
    breakdown = today_summary.get("market_breakdown", {})
    total_volume = breakdown.get("total_volume_va", 0) or 0
    if not total_volume:
        st.info("JPX公式内訳データがありません。")
        return

    short_with = breakdown.get("shrt_with_res_va", 0) or 0
    short_without = breakdown.get("shrt_no_res_va", 0) or 0
    actual = breakdown.get("sell_ex_short_va", 0) or 0
    total_short = breakdown.get("total_short_va", short_with + short_without) or 0

    cols = st.columns(4)
    cols[0].metric("実注文", _pct(actual / total_volume * 100))
    cols[1].metric("価格規制あり", _pct(short_with / total_volume * 100))
    cols[2].metric("価格規制なし", _pct(short_without / total_volume * 100))
    cols[3].metric("規制なし構成比", _pct(short_without / total_short * 100 if total_short else 0))

    df = pd.DataFrame([
        {"category": "実注文", "value": actual},
        {"category": "価格規制あり", "value": short_with},
        {"category": "価格規制なし", "value": short_without},
    ])
    fig = px.pie(df, names="category", values="value", title="JPX空売り内訳")
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)

    signals = today_summary.get("flow_signals", [])
    if signals:
        st.subheader("機械判定シグナル")
        st.dataframe(pd.DataFrame(signals), hide_index=True, use_container_width=True)


def _render_market_theme_tab(selected_date: str, today_summary: dict) -> None:
    st.subheader("市場テーマ調査")
    manual_news = st.text_area(
        "今日の市場メモ / 追加ニュース",
        key=f"market_memo_{selected_date}",
        height=150,
        placeholder="例: 米10年金利上昇、ドル円変動、SOX下落、ホルムズ海峡リスク後退など",
    )
    auto_fetch_news = st.checkbox(
        "Tavilyでニュース取得を試す",
        value=False,
        help="TAVILY_API_KEYが設定されている場合だけ外部APIを呼びます。",
    )

    if auto_fetch_news and not TAVILY_API_KEY:
        st.warning("TAVILY_API_KEY が未設定です。チェックしてもニュース取得はスキップされます。")

    if st.button("市場テーマを判定", use_container_width=True):
        with st.spinner("市場テーマを組み立て中..."):
            bundle = build_market_context_bundle(
                target_date=selected_date,
                today_summary=today_summary,
                manual_news=manual_news,
                auto_fetch_news=auto_fetch_news,
            )
            theme_dicts = build_theme_snapshot_dicts(
                selected_date,
                today_summary,
                manual_news=bundle.combined_news_text,
            )
            save_market_theme_snapshots(selected_date, theme_dicts)
            save_market_news_snapshots(
                selected_date,
                [item.to_dict() for item in bundle.fetched_news],
            )
        st.session_state[f"context_preview_{selected_date}"] = bundle.to_prompt_block()
        st.success("市場テーマ判定を保存しました。")

    preview = st.session_state.get(f"context_preview_{selected_date}")
    if preview:
        st.code(preview, language="markdown")

    saved_themes = get_market_theme_snapshots(selected_date)
    if saved_themes:
        st.subheader("保存済みテーマ判定")
        st.dataframe(pd.DataFrame(saved_themes), hide_index=True, use_container_width=True)

    saved_news = get_market_news_snapshots(selected_date)
    if saved_news:
        st.subheader("保存済みニュース")
        st.dataframe(pd.DataFrame(saved_news), hide_index=True, use_container_width=True)

    _render_market_theme_history(selected_date)


def _render_market_theme_history(selected_date: str) -> None:
    st.subheader("市場テーマ履歴")
    theme_dates_desc = get_market_theme_snapshot_dates(limit=30)
    if not theme_dates_desc:
        st.info("市場テーマ履歴はまだありません。")
        return

    theme_dates = sorted(theme_dates_desc)
    snapshots_by_date = {
        date_value: get_market_theme_snapshots(date_value)
        for date_value in theme_dates
    }
    current_themes = snapshots_by_date.get(selected_date, [])
    previous_date = find_previous_theme_date(theme_dates, selected_date)
    previous_themes = snapshots_by_date.get(previous_date, []) if previous_date else []

    compare_col, trend_col = st.columns([1, 1])
    with compare_col:
        st.caption(
            f"比較対象: {selected_date}"
            + (f" vs {previous_date}" if previous_date else "（前回データなし）")
        )
        if current_themes or previous_themes:
            comparison_rows = build_theme_comparison_rows(current_themes, previous_themes)
            st.dataframe(
                pd.DataFrame(comparison_rows),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("選択日の市場テーマ判定はまだ保存されていません。")

    history_rows = build_theme_history_rows(snapshots_by_date)
    if not history_rows:
        return

    history_df = pd.DataFrame(history_rows)
    with trend_col:
        visible_themes = _select_history_theme_names(history_df, current_themes)
        chart_df = history_df[history_df["name"].isin(visible_themes)]
        if not chart_df.empty:
            fig = px.line(
                chart_df,
                x="date",
                y="score",
                color="name",
                markers=True,
                title="市場テーマ スコア推移",
            )
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        history_df.sort_values(["date", "score"], ascending=[False, False]),
        hide_index=True,
        use_container_width=True,
    )


def _select_history_theme_names(history_df: pd.DataFrame, current_themes: list[dict]) -> list[str]:
    current_names = [theme.get("name") for theme in current_themes if theme.get("name")]
    if current_names:
        return current_names[:5]
    latest_date = history_df["date"].max()
    latest = history_df[history_df["date"] == latest_date].sort_values(
        "score", ascending=False
    )
    return latest["name"].head(5).tolist()


def _render_ai_report_tab(
    selected_date: str,
    today_summary: dict,
    weekly_df: pd.DataFrame,
    anomalies: list,
) -> None:
    manual_news = st.text_area(
        "AIレポート用 追加ニュース/市場メモ",
        key=f"ai_memo_{selected_date}",
        height=120,
        placeholder="市場テーマタブと同じ材料を入れると、レポート冒頭のテーマ判定に反映されます。",
    )
    auto_fetch_news = st.checkbox(
        "AIレポート生成時にTavilyニュース取得を試す",
        value=False,
        key=f"ai_auto_news_{selected_date}",
    )
    stored_report = get_ai_report(selected_date)
    quality_feedback_preview = _build_quality_feedback_for_regeneration(
        stored_report,
        selected_date,
        today_summary,
    )
    use_quality_feedback = _render_quality_feedback_preview(
        selected_date,
        quality_feedback_preview,
    )

    if st.button("Gemini AIレポートを生成", type="primary", use_container_width=True):
        with st.spinner("Geminiでレポート生成中..."):
            quality_feedback = quality_feedback_preview if use_quality_feedback else ""
            generator = GeminiReportGenerator()
            report_obj, markdown = generator.generate_report(
                selected_date,
                today_summary,
                weekly_df,
                anomalies,
                extra_news=manual_news,
                auto_fetch_news=auto_fetch_news,
                quality_feedback=quality_feedback,
            )
            save_ai_report(
                selected_date,
                report_obj.current_macro_context,
                markdown,
                report_json=report_obj.model_dump_json(ensure_ascii=False),
                model_used=GEMINI_MODEL,
            )
        st.success("AIレポートを保存しました。")
        st.rerun()

    if stored_report:
        _render_report_quality_panel(stored_report, today_summary)
        st.markdown(stored_report.report_markdown)
        st.download_button(
            "Markdownをダウンロード",
            data=stored_report.report_markdown.encode("utf-8"),
            file_name=f"short_ratio_report_{selected_date}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    else:
        st.info("この日付のAIレポートはまだ生成されていません。")


def _render_quality_feedback_preview(selected_date: str, quality_feedback: str) -> bool:
    if not quality_feedback:
        return False

    with st.expander("再生成時にGeminiへ渡す改善メモ", expanded=False):
        st.caption("前回保存レポートの品質チェック結果から自動生成した改善指示です。")
        st.code(quality_feedback, language="markdown")
        return st.checkbox(
            "この改善メモを再生成プロンプトに反映する",
            value=True,
            key=f"use_quality_feedback_{selected_date}",
        )


def _build_quality_feedback_for_regeneration(
    previous_report,
    selected_date: str,
    today_summary: dict,
) -> str:
    if previous_report is None:
        return ""

    theme_transition_context = build_theme_transition_context_for_prompt(
        target_date=selected_date,
        today_summary=today_summary,
    )
    quality = evaluate_report_quality(
        previous_report.report_markdown,
        getattr(previous_report, "report_json", "") or "",
        theme_transition_context=theme_transition_context,
    )
    return build_quality_feedback_prompt_block(quality)


def _render_report_quality_panel(stored_report, today_summary: dict) -> None:
    theme_transition_context = build_theme_transition_context_for_prompt(
        target_date=stored_report.date,
        today_summary=today_summary,
    )
    quality = evaluate_report_quality(
        stored_report.report_markdown,
        getattr(stored_report, "report_json", "") or "",
        theme_transition_context=theme_transition_context,
    )
    failed_rows = quality.to_rows(include_passed=False)

    with st.expander("AIレポート品質チェック", expanded=True):
        cols = st.columns(4)
        cols[0].metric("判定", quality.status_label)
        cols[1].metric("スコア", f"{quality.score_pct:.1f}%")
        cols[2].metric("重大", quality.high_count)
        cols[3].metric("要確認", len(quality.failed_items))

        if failed_rows:
            st.dataframe(pd.DataFrame(failed_rows), hide_index=True, use_container_width=True)
        else:
            st.success("品質チェックで重大な問題は検出されませんでした。")

        show_passed = st.checkbox(
            "通過項目も表示",
            value=False,
            key=f"quality_show_passed_{stored_report.date}",
        )
        if show_passed:
            st.dataframe(
                pd.DataFrame(quality.to_rows(include_passed=True)),
                hide_index=True,
                use_container_width=True,
            )


def _render_history_tab(selected_date: str) -> None:
    st.subheader("保存済みデータ")
    data_dates = get_saved_short_ratio_dates()
    st.write(f"空売りデータ: {len(data_dates)}日")
    if data_dates:
        st.write(f"範囲: {min(data_dates)} 〜 {max(data_dates)}")

    report_dates = get_ai_report_dates()
    st.write(f"AIレポート: {len(report_dates)}本")
    if report_dates:
        selected_report_date = st.selectbox("レポート履歴", report_dates)
        report = get_ai_report(selected_report_date)
        if report:
            st.markdown(report.report_markdown)


def _sector_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    columns = ["sector_name", "short_ratio_pct", "dod_change", "zone_label"]
    return df[[col for col in columns if col in df.columns]]


def _show_fetch_result(result: dict) -> None:
    st.success(
        f"{result.get('target_date')} / "
        f"業種 {result.get('saved_sector', 0)}件 / "
        f"市場全体 {result.get('saved_market', 0)}件"
    )
    st.caption(
        f"sector_source={result.get('sector_source')} / "
        f"market_source={result.get('market_source')}"
    )


def _pct(value) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _pt(value) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):+.1f}pt"
    except (TypeError, ValueError):
        return None


def _apply_style() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 3rem;
            max-width: 1440px;
        }
        div[data-testid="stMetric"] {
            border: 1px solid #d7dde6;
            border-radius: 8px;
            padding: 12px 14px;
            background: #fbfcfe;
        }
        div[data-testid="stMetric"] label {
            color: #526070;
        }
        h1, h2, h3 {
            letter-spacing: 0;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 6px;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 6px 6px 0 0;
            padding: 10px 14px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
