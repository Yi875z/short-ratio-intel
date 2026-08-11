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

from config.settings import (
    GEMINI_MODEL,
    MARKET_NEWS_AUTO_FETCH,
    MARKET_NEWS_RSS_ENABLED,
    TAVILY_API_KEY,
)
from src.ai_engine.gemini_client import GeminiReportGenerator
from src.macro_context.event_calendar import (
    earnings_season_label,
    get_events_for_date,
    get_events_for_month,
)
from src.macro_context.house_view import load_house_view, store_house_view
from src.macro_context.institutional_flow import diagnose_connection, fetch_investor_flow
from src.macro_context.market_quotes import fetch_nt_ratio_history, fetch_quotes
from src.ai_engine.prompt_builder import build_theme_transition_context_for_prompt
from src.ai_engine.report_quality import (
    build_quality_comparison,
    build_quality_comparison_markdown,
    build_quality_feedback_prompt_block,
    build_quality_history_row,
    build_quality_review_markdown,
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
    load_ai_report_quality_comparison,
    get_market_news_snapshots,
    get_market_short_ratio_df,
    get_market_theme_snapshot_dates,
    get_market_theme_snapshots,
    get_saved_short_ratio_dates,
    save_ai_report,
    save_ai_report_quality_comparison,
    save_market_news_snapshots,
    save_market_theme_snapshots,
    upsert_market_short_ratio_records,
    upsert_short_ratio_records,
)
# 米国ショートフロー（US-P2）。日本側の描画とは独立しており、
# データが無くてもこのタブ内で完結して案内を出す。
from src.storage.db import get_us_market_daily_df, get_us_short_volume_df
from src.report.us_daily_report import build_daily_report
from src.analyzer.us_flow_classifier import PATTERN_LABELS
from config.us_universe import TICKER_GROUP, ai_category, japanese_name


AUTO_FETCH_DAYS = 5


def _require_login() -> None:
    """Streamlit Cloud の公開URLを bcrypt でログイン保護する。

    secrets.toml に [auth] セクションが無い場合（ローカル開発など）は
    認証なしでそのまま通す。Cloud では [auth] を設定して保護を有効化する。
    """
    try:
        has_auth = "auth" in st.secrets
    except Exception:  # secrets ファイル自体が無い環境
        has_auth = False
    if not has_auth or st.session_state.get("authenticated"):
        return

    cfg = st.secrets["auth"]
    st.title("空売り比率インテリジェンス")
    with st.form("login"):
        st.subheader("ログイン")
        username = st.text_input("ユーザー名")
        password = st.text_input("パスワード", type="password")
        if st.form_submit_button("ログイン"):
            import bcrypt

            ok = username == cfg.get("username") and bcrypt.checkpw(
                password.encode(), str(cfg.get("password_hash", "")).encode()
            )
            if ok:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("ユーザー名またはパスワードが違います")
    st.stop()


def main() -> None:
    st.set_page_config(
        page_title="空売り比率インテリジェンス",
        layout="wide",
    )
    _apply_style()
    _require_login()

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

    (
        overview_tab,
        sectors_tab,
        breakdown_tab,
        theme_tab,
        market_data_tab,
        calendar_tab,
        report_tab,
        history_tab,
        us_flow_tab,
    ) = st.tabs(
        ["概要", "業種", "JPX内訳", "市場テーマ", "🌐 市場データ", "📅 カレンダー", "AIレポート", "履歴",
         "🇺🇸 米国ショート"]
    )

    with overview_tab:
        _render_overview(selected_date, today_summary, market_trend_df, anomalies)
    with sectors_tab:
        _render_sectors(today_summary, weekly_df)
    with breakdown_tab:
        _render_breakdown(today_summary)
    with theme_tab:
        _render_market_theme_tab(selected_date, today_summary)
    with market_data_tab:
        _render_market_data_tab()
    with calendar_tab:
        _render_calendar_tab(selected_date)
    with report_tab:
        _render_ai_report_tab(selected_date, today_summary, weekly_df, anomalies)
    with history_tab:
        _render_history_tab(selected_date)
    with us_flow_tab:
        _render_us_flow_tab()


def _shift_ym(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _cal_html_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cal_html_attr(s: str) -> str:
    return _cal_html_text(s).replace('"', "&quot;")


def _chip_colors(e) -> tuple[str, str]:
    """イベントチップの (背景色, 文字色)。指数イベントはフェーズ別、他は地域別。"""
    if e.phase == "passive_trade":
        return "#fdebd0", "#b9770e"   # 橙: パッシブ実売買日（需給インパクト最大）
    if e.phase == "effective":
        return "#d5f5e3", "#1e8449"   # 緑: 指数発効日
    if e.phase in ("announcement", "base_date", "watch"):
        return "#eaecee", "#566573"   # 灰: 発表・基準・ウォッチ
    if e.category in ("sq", "rollover"):
        return "#ebdef0", "#7d3c98"   # 紫: SQ・先物ロール
    if e.region == "JP":
        return "#fdecea", "#c0392b"   # 赤: 日本マクロ
    return "#e8f0fb", "#1f5fa8"       # 青: 米国マクロ


def _build_calendar_html(year: int, month: int, by_day: dict, highlight) -> str:
    """月間カレンダーのHTMLを生成する（日曜始まり・イベントチップ付き）。"""
    import calendar as _cal

    weeks = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)
    head = ["日", "月", "火", "水", "木", "金", "土"]
    parts = ["<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>", "<tr>"]
    for i, label in enumerate(head):
        color = "#c0392b" if i == 0 else ("#2c6fbb" if i == 6 else "#333")
        parts.append(
            f"<th style='border:1px solid #ddd;padding:4px;background:#f5f5f5;"
            f"color:{color};font-size:12px;'>{label}</th>"
        )
    parts.append("</tr>")

    for week in weeks:
        parts.append("<tr>")
        for i, day in enumerate(week):
            if day == 0:
                parts.append("<td style='border:1px solid #eee;height:92px;background:#fafafa;'></td>")
                continue
            is_anchor = (
                highlight is not None
                and day == highlight.day
                and month == highlight.month
                and year == highlight.year
            )
            day_color = "#c0392b" if i == 0 else ("#2c6fbb" if i == 6 else "#999")
            cell_bg = "#fffbe6" if is_anchor else "#fff"
            border = "2px solid #f1c40f" if is_anchor else "1px solid #ddd"
            chips = []
            for e in by_day.get(day, []):
                bg, fg = _chip_colors(e)
                star = "★" if e.importance == "high" else ""
                phase = f"[{e.phase_label()}]" if e.phase != "event" else ""
                name = e.name if len(e.name) <= 16 else e.name[:15] + "…"
                tooltip = f"{phase} {e.name} — {e.note}".strip()
                chips.append(
                    f"<div title=\"{_cal_html_attr(tooltip)}\" "
                    f"style='font-size:10px;margin:1px 0;padding:1px 3px;border-radius:3px;"
                    f"background:{bg};color:{fg};white-space:nowrap;overflow:hidden;"
                    f"text-overflow:ellipsis;'>{star}{_cal_html_text(phase + name)}</div>"
                )
            parts.append(
                f"<td style='border:{border};height:92px;vertical-align:top;padding:2px;"
                f"background:{cell_bg};'>"
                f"<div style='font-size:11px;text-align:right;color:{day_color};'>{day}</div>"
                f"{''.join(chips)}</td>"
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_market_quotes():
    """主要市場のライブ気配を5分キャッシュで取得（Streamlit Cloudのレート制限対策）。

    戻り値: (取得時刻JST文字列, quotesリスト)。
    """
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    fetched_at = datetime.now(jst).strftime("%Y-%m-%d %H:%M")
    return fetched_at, fetch_quotes()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_nt_ratio_history(period: str = "6mo"):
    """NT倍率（日経平均÷TOPIX）の推移を5分キャッシュで取得する。"""
    return fetch_nt_ratio_history(period)


def _short_as_of(as_of: str) -> str:
    """カード表示用にデータ時点を短縮（準リアルタイムは時刻、日次は M/D）。"""
    if not as_of:
        return ""
    if " " in as_of:  # "06-04 22:41"（準リアルタイム）→ 時刻
        return as_of.split(" ", 1)[1]
    parts = as_of.split("-")  # "2026-06-04" → "6/4"
    if len(parts) == 3:
        return f"{int(parts[1])}/{int(parts[2])}"
    return as_of


def _render_market_data_tab() -> None:
    """日経/TOPIX/ナスダック先物・ドル円・主要海外指標のライブ気配を表示する。"""
    st.subheader("🌐 主要金融市場データ")
    st.caption(
        "AIレポート生成前にボタンで最新気配を確認できます。"
        "取得した実測値はAIレポートにも自動で注入されます（5分キャッシュ）。"
    )

    if st.button("市場データを取得", type="primary", use_container_width=True):
        _cached_market_quotes.clear()
        _cached_nt_ratio_history.clear()

    with st.spinner("市場データを取得中..."):
        fetched_at, quotes = _cached_market_quotes()

    st.caption(f"🕒 取得時刻: {fetched_at}（JST）／日経先物・ドル円は約10分遅れの準リアルタイム")

    ok_quotes = [q for q in quotes if q.ok]
    if not ok_quotes:
        st.error(
            "市場データを取得できませんでした。時間をおいて再試行してください"
            "（データ提供元の一時的な不調の可能性）。"
        )
        with st.expander("取得状況（診断）", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [{"銘柄": q.label, "ticker": q.ticker, "エラー": q.error} for q in quotes]
                ),
                use_container_width=True,
                hide_index=True,
            )
        return

    # カテゴリ順を維持して metric を並べる
    categories: list[str] = []
    for q in quotes:
        if q.category not in categories:
            categories.append(q.category)

    for category in categories:
        cat_quotes = [q for q in quotes if q.category == category]
        st.markdown(f"#### {category}")
        cols = st.columns(min(len(cat_quotes), 4))
        for i, q in enumerate(cat_quotes):
            with cols[i % len(cols)]:
                delta = q.change_text if q.ok else None
                label = f"{q.label}　🕒{_short_as_of(q.as_of)}" if q.as_of else q.label
                st.metric(label=label, value=q.value_text, delta=delta)

    st.caption(
        "出所: nikkei225jp.com（日経VIのみ stock-marketdata.com）。各カードの🕒はそのデータ時点"
        "（時刻=準リアルタイム、日付=その営業日の終値）。日経先物は大取・CME、前日比は前日終値（清算値）比。"
    )

    # NT倍率（日経平均 ÷ TOPIX）の推移
    st.markdown("#### NT倍率（日経平均 ÷ TOPIX）の推移")
    nt_df = _cached_nt_ratio_history("6mo")
    if nt_df is None or nt_df.empty:
        st.caption("NT倍率データを取得できませんでした（時間をおいて再試行してください）。")
    else:
        latest = float(nt_df["nt_ratio"].iloc[-1])
        prev = float(nt_df["nt_ratio"].iloc[-2]) if len(nt_df) >= 2 else None
        delta = f"{latest - prev:+.3f}" if prev is not None else None
        st.metric("現在のNT倍率", f"{latest:.2f}", delta=delta)
        fig = px.line(nt_df, x="date", y="nt_ratio", title="NT倍率の推移（直近6ヶ月）")
        fig.update_layout(yaxis_title="NT倍率", xaxis_title="", height=320)
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "NT倍率＝日経平均÷TOPIX。高いほど値がさ/グロース優位、低いほど内需/バリュー優位。"
            "出所: nikkei225jp.com（実日経平均・実TOPIXの日次値）。"
        )


def _render_calendar_tab(selected_date: str) -> None:
    """月間の市場イベント・カレンダーを中央に表示する（サイドバー一覧と併存）。"""
    from datetime import date, datetime

    st.subheader("📅 市場イベント・カレンダー（月間）")
    st.caption(
        "SQ・先物ロール・指数リバランス（MSCI/FTSE/日経平均/TOPIX/JPX日経400）・配当落ち・"
        "FOMC・日銀会合/短観・米CPI/PPI/雇用統計/PCE/GDP/小売/ISM・日本CPIなどの予定"
        "（2026年は公式確定日）。指数イベントは発表→実売買→発効のフェーズ別に表示。"
        "AIレポートにも同じカレンダー情報が反映されます。"
    )

    try:
        anchor = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        anchor = date.today()
    if "cal_year" not in st.session_state:
        st.session_state.cal_year = anchor.year
        st.session_state.cal_month = anchor.month

    c1, c2, c3 = st.columns([1, 3, 1])
    with c1:
        if st.button("← 前月", key="cal_prev", use_container_width=True):
            st.session_state.cal_year, st.session_state.cal_month = _shift_ym(
                st.session_state.cal_year, st.session_state.cal_month, -1
            )
    with c3:
        if st.button("翌月 →", key="cal_next", use_container_width=True):
            st.session_state.cal_year, st.session_state.cal_month = _shift_ym(
                st.session_state.cal_year, st.session_state.cal_month, 1
            )
    with c2:
        st.markdown(
            f"<h3 style='text-align:center;margin:0;'>"
            f"{st.session_state.cal_year}年 {st.session_state.cal_month}月</h3>",
            unsafe_allow_html=True,
        )

    year, month = st.session_state.cal_year, st.session_state.cal_month
    events = get_events_for_month(year, month)
    by_day: dict[int, list] = {}
    for e in events:
        by_day.setdefault(e.event_date.day, []).append(e)

    st.markdown(_build_calendar_html(year, month, by_day, anchor), unsafe_allow_html=True)
    st.caption(
        "🟧 実売買（パッシブ需給の集中日） ／ 🟩 発効 ／ ⬜ 発表・基準・ウォッチ ／ "
        "🟪 SQ・ロール ／ 🟥 日本マクロ ／ 🟦 米国マクロ　｜　★=重要度high　｜　"
        "黄枠=分析日　｜　チップにカーソルを乗せると詳細"
    )

    if events:
        with st.expander("この月のイベント一覧（テキスト）", expanded=False):
            for e in events:
                phase = f"・{e.phase_label()}" if e.phase != "event" else ""
                st.markdown(
                    f"- **{e.event_date.isoformat()}**（{e.region}{phase}・"
                    f"{'★high' if e.importance == 'high' else e.importance}） "
                    f"{e.name} — {e.note}"
                )


def _render_sidebar_event_calendar(target_date: str) -> None:
    """サイドバーに対象日基準の市場イベント（MSCI入替/SQ/FOMC/日銀等）を常時表示する。

    レポートと並べて確認できるよう、AIに渡すカレンダーと同じ内容をユーザーにも見せる。
    """
    from datetime import datetime

    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return

    events = get_events_for_date(target_date)
    season = earnings_season_label(target)
    with st.expander("📅 市場イベント（対象日基準）", expanded=False):
        if season:
            st.caption(f"決算期: {season}")
        if not events:
            st.caption("対象日前後に主要な予定イベントは検出されず。")
            return
        for e in events:
            if e.importance == "low":
                continue
            mark = "🔴" if e.importance == "high" else "▫️"
            phase = f"・{e.phase_label()}" if e.phase != "event" else ""
            st.markdown(
                f"{mark} **{e.event_date.isoformat()}**"
                f"（{e.relation_label(target)}・{e.region}{phase}）  \n{e.name}",
                help=e.note,
            )


@st.cache_data(ttl=600, show_spinner=False)
def _cached_knowledge_meta():
    """配信ナレッジ（Supabase knowledge_documents）のメタ情報を10分キャッシュで取得。"""
    from src.storage.db import get_knowledge_document_meta

    return get_knowledge_document_meta()


def _render_sidebar_knowledge_freshness() -> None:
    """配信ナレッジの更新日時を表示し、ローカル原本と差分があれば警告する。

    2026-07-04に「00プロトコルが旧版のまま配信されていた」事故が見つかったため、
    原本更新→再アップロード忘れを画面で検知できるようにする。
    """
    from datetime import timedelta, timezone

    from config.settings import EXTERNAL_KNOWLEDGE_DIR
    from src.knowledge.loader import EXTERNAL_KNOWLEDGE_FILES

    meta = _cached_knowledge_meta()
    if not meta:
        return
    jst = timezone(timedelta(hours=9))
    with st.expander("🧠 ナレッジ鮮度（AIレポートの知識）", expanded=False):
        stale_keys = []
        for m in meta:
            if str(m["key"]).startswith("__"):  # __house_view__ 等の予約キーは対象外
                continue
            updated = m["updated_at"]
            label = (
                updated.replace(tzinfo=timezone.utc).astimezone(jst).strftime("%m/%d %H:%M")
                if updated is not None
                else "-"
            )
            mark = ""
            filename = EXTERNAL_KNOWLEDGE_FILES.get(m["key"], "")
            local_path = EXTERNAL_KNOWLEDGE_DIR / filename if filename else None
            if local_path is not None and local_path.exists():
                try:
                    if len(local_path.read_text(encoding="utf-8")) != m["chars"]:
                        mark = "　⚠️ 原本と差分"
                        stale_keys.append(m["key"])
                except OSError:
                    pass
            st.caption(f"{m['key']}: {label} JST・{m['chars']:,}字{mark}")
        if stale_keys:
            st.warning(
                "ローカル原本と配信ナレッジに差分があります。"
                "`python -m scripts.upload_knowledge_to_supabase` で再アップロードしてください。"
            )
        else:
            st.caption("※ ⚠️が無ければ配信ナレッジは原本と同期しています（原本があるPCのみ判定）。")


def _sidebar() -> str | None:
    with st.sidebar:
        st.header("操作")
        dates = get_saved_short_ratio_dates()
        selected_date = dates[0] if dates else None

        if dates:
            selected_date = st.selectbox("分析日", dates, index=0)

        manual_date = st.date_input("取得日", value=date.today())
        target_preview_date = manual_date.strftime("%Y-%m-%d")
        if dates:
            st.caption(f"DB最新保存日: {dates[0]}")

        with st.expander("取得見込みチェック", expanded=False):
            st.caption("指定日のデータが公開済みか、DBへ保存する前に確認します。")
            if st.button("指定日の取得可否を確認", use_container_width=True):
                with st.spinner(f"{target_preview_date} の公開状況を確認中..."):
                    availability = check_short_ratio_source_availability(
                        target_preview_date,
                        saved_dates=dates,
                    )
                st.session_state[f"fetch_availability_{target_preview_date}"] = availability

            availability = st.session_state.get(
                f"fetch_availability_{target_preview_date}"
            )
            if availability:
                _show_fetch_availability(availability)

        if st.button("指定日を取得", use_container_width=True):
            target = target_preview_date
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
        st.caption("ニュース取得")
        st.write(f"RSS(ロイター/日経/Bloomberg/GoogleNews): {'ON' if MARKET_NEWS_RSS_ENABLED else 'OFF'}")
        st.caption("RSSは無料・対象日スコープで常時取得（既定ON）")
        st.write(f"Tavily(補助): {'設定済み' if TAVILY_API_KEY else '未設定'}")

        if selected_date:
            st.divider()
            _render_sidebar_event_calendar(selected_date)

        _render_sidebar_knowledge_freshness()

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


def check_short_ratio_source_availability(
    target_date: str,
    saved_dates: list[str] | None = None,
) -> dict:
    """指定日の取得可否をDB書き込みなしで確認する。"""
    saved_dates = saved_dates if saved_dates is not None else get_saved_short_ratio_dates()
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

    sector_count = len(sector_records)
    market_available = bool(market_record)
    can_fetch = bool(sector_count and market_available)
    partial = bool((sector_count or market_available) and not can_fetch)

    if can_fetch:
        status = "取得可能"
        if target_date in saved_dates:
            message = "DB保存済みです。再取得すると公開元の最新データで更新できます。"
        else:
            message = "業種別データと東証全体データの両方が公開元で確認できました。"
    elif partial:
        status = "一部取得可能"
        message = "業種別データまたは東証全体データのどちらかが未取得です。保存前に再確認してください。"
    else:
        status = "未公開または取得不可"
        message = "現時点では公開元に対象日データが見つかりません。公開待ち、非営業日、通信制限の可能性があります。"

    return {
        "target_date": target_date,
        "saved_in_db": target_date in saved_dates,
        "status": status,
        "message": message,
        "can_fetch": can_fetch,
        "partial": partial,
        "sector_count": sector_count,
        "market_available": market_available,
        "sector_source": sector_source if sector_count else "none",
        "market_source": market_source if market_available else "none",
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


def _render_house_view_editor() -> None:
    """運用者ハウスビュー（全レポート共通の相場観アンカー）の表示・編集。

    平日19時の自動レポートもこの保存値を使うため、相場観が変わったら更新する。
    """
    hv = load_house_view()
    has_view = bool(hv and hv.content.strip())
    with st.expander("🧭 運用者ハウスビュー（相場観アンカー｜全レポート共通）", expanded=not has_view):
        if has_view:
            if hv.is_stale():
                st.warning(
                    f"⚠️ 最終更新 {hv.updated_label()}（約{hv.age_days}日前）。"
                    "古い見解で自動レポートが生成され続けないよう、更新を推奨します。"
                )
            else:
                st.caption(f"最終更新 {hv.updated_label()}")
        else:
            st.info(
                "未設定です。現在の相場観を入力すると、AIレポートの支配的マクロ背景の"
                "起点になり、当日ニュースと突合されます（古い固定文より優先）。"
            )
        text = st.text_area(
            "現在の相場観（主役テーマ・金利/為替/原油観・注目セクター・想定リスク等）",
            value=hv.content if has_view else "",
            height=160,
            key="house_view_editor",
            help="平日19時の自動レポートもこの内容を使います。相場観が変わったら更新してください。",
        )
        if st.button("ハウスビューを保存", key="save_house_view"):
            store_house_view(text)
            st.success("ハウスビューを保存しました。次回レポートから反映されます。")
            st.rerun()


def _render_institutional_flow_section(selected_date: str) -> None:
    """投資主体別フロー（jpx-analysis 週次）の生データをレポートと並べて表示する。"""
    snap = fetch_investor_flow(selected_date)
    with st.expander("🏦 投資主体別フロー（週次・jpx-analysis連携）", expanded=False):
        if snap is None or not snap.flows:
            st.caption(
                "未接続または取得失敗。下の接続診断で原因を確認してください"
                "（秘密値は表示しません）。"
            )
            diag = diagnose_connection(selected_date)
            st.write(
                {
                    "URL設定": diag["url_set"],
                    "KEY設定": diag["key_set"],
                    "接続先ホスト": diag["url_host"],
                    "KEY先頭": diag["key_prefix"],
                    "HTTPステータス": diag["status"],
                    "取得件数": diag["rows"],
                    "エラー": diag["error"],
                }
            )
            st.caption("保存済みSecret名（値は含みません。ここに JPX_ANALYSIS_SUPABASE_URL / KEY があるか確認）:")
            st.write(diag.get("保存済みSecret名") or "（取得不可）")
            return
        st.caption(f"{snap.week_date} 時点・単位:億円（+買い越し / −売り越し）")
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "投資主体": f.label,
                    "現物net": round(f.spot_net),
                    "先物net": round(f.futures_net_oku),
                    "合算": round(f.combined_net),
                    "ツインエンジン": "○" if f.is_twin_engine else "",
                }
                for f in snap.flows
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)


def _render_ai_report_tab(
    selected_date: str,
    today_summary: dict,
    weekly_df: pd.DataFrame,
    anomalies: list,
) -> None:
    _render_house_view_editor()
    _render_institutional_flow_section(selected_date)
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
            before_quality_row = (
                _build_report_quality_row_from_markdown(
                    report_date=selected_date,
                    markdown=stored_report.report_markdown,
                    report_json=getattr(stored_report, "report_json", "") or "",
                    today_summary=today_summary,
                    model_used=getattr(stored_report, "model_used", "") or "",
                    generated_at=getattr(stored_report, "generated_at", None),
                )
                if stored_report
                else None
            )
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
            report_json = report_obj.model_dump_json()
            after_quality_row = _build_report_quality_row_from_markdown(
                report_date=selected_date,
                markdown=markdown,
                report_json=report_json,
                today_summary=today_summary,
                model_used=GEMINI_MODEL,
            )
            quality_comparison = build_quality_comparison(before_quality_row, after_quality_row)
            quality_comparison_markdown = build_quality_comparison_markdown(quality_comparison)
            save_ai_report(
                selected_date,
                report_obj.current_macro_context,
                markdown,
                report_json=report_json,
                model_used=GEMINI_MODEL,
            )
            save_ai_report_quality_comparison(selected_date, quality_comparison_markdown)
            st.session_state[f"quality_regen_comparison_{selected_date}"] = quality_comparison
            st.session_state[f"quality_regen_comparison_md_{selected_date}"] = (
                quality_comparison_markdown
            )
        st.success("AIレポートを保存しました。")
        st.rerun()

    if stored_report:
        _render_quality_regeneration_comparison(selected_date)
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

        quality_feedback = build_quality_feedback_prompt_block(quality)
        quality_rows_csv = pd.DataFrame(
            quality.to_rows(include_passed=True)
        ).to_csv(index=False).encode("utf-8-sig")
        quality_review_md = build_quality_review_markdown(
            quality,
            report_date=stored_report.date,
            quality_feedback=quality_feedback,
        )
        dl_cols = st.columns(2)
        dl_cols[0].download_button(
            "品質チェックCSVをダウンロード",
            data=quality_rows_csv,
            file_name=f"ai_report_quality_{stored_report.date}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        dl_cols[1].download_button(
            "品質レビューMarkdownをダウンロード",
            data=quality_review_md.encode("utf-8"),
            file_name=f"ai_report_quality_review_{stored_report.date}.md",
            mime="text/markdown",
            use_container_width=True,
        )


def _render_quality_regeneration_comparison(selected_date: str) -> None:
    comparison = st.session_state.get(f"quality_regen_comparison_{selected_date}")
    saved_markdown = (
        st.session_state.get(f"quality_regen_comparison_md_{selected_date}")
        or load_ai_report_quality_comparison(selected_date)
    )
    if not comparison:
        if saved_markdown:
            with st.expander("保存済み品質比較レビュー", expanded=False):
                st.markdown(saved_markdown)
                st.download_button(
                    "品質比較レビューMarkdownをダウンロード",
                    data=saved_markdown.encode("utf-8"),
                    file_name=f"ai_report_quality_comparison_{selected_date}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )
        return

    with st.expander("前回再生成の品質比較", expanded=True):
        cols = st.columns(4)
        cols[0].metric("結果", comparison.get("result", ""))
        cols[1].metric(
            "スコア",
            _comparison_value(comparison.get("after_score_pct"), suffix="%"),
            _comparison_delta(comparison.get("score_delta"), suffix="pt"),
        )
        cols[2].metric(
            "未通過",
            _comparison_value(comparison.get("after_failed_count"), suffix="件"),
            _comparison_delta(comparison.get("failed_delta"), suffix="件"),
            delta_color="inverse",
        )
        cols[3].metric(
            "重大",
            _comparison_value(comparison.get("after_high_count"), suffix="件"),
            _comparison_delta(comparison.get("high_delta"), suffix="件"),
            delta_color="inverse",
        )

        display_row = {
            "日付": comparison.get("date", ""),
            "結果": comparison.get("result", ""),
            "再生成前判定": comparison.get("before_status", ""),
            "再生成後判定": comparison.get("after_status", ""),
            "再生成前スコア": comparison.get("before_score_pct"),
            "再生成後スコア": comparison.get("after_score_pct"),
            "スコア差分": comparison.get("score_delta"),
            "未通過差分": comparison.get("failed_delta"),
            "重大差分": comparison.get("high_delta"),
        }
        comparison_df = pd.DataFrame([display_row])
        st.dataframe(comparison_df, hide_index=True, use_container_width=True)
        st.download_button(
            "品質比較CSVをダウンロード",
            data=comparison_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"ai_report_quality_comparison_{selected_date}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        if saved_markdown:
            st.download_button(
                "品質比較レビューMarkdownをダウンロード",
                data=saved_markdown.encode("utf-8"),
                file_name=f"ai_report_quality_comparison_{selected_date}.md",
                mime="text/markdown",
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
        _render_report_quality_history(report_dates)

        selected_report_date = st.selectbox("レポート履歴", report_dates)
        report = get_ai_report(selected_report_date)
        if report:
            st.markdown(report.report_markdown)


def _render_report_quality_history(report_dates: list[str]) -> None:
    st.subheader("AIレポート品質履歴")
    quality_rows = _build_report_quality_history_rows(report_dates)
    if not quality_rows:
        st.info("品質履歴を作成できるAIレポートがありません。")
        return

    quality_df = pd.DataFrame(quality_rows)
    latest = quality_df.iloc[0]
    cols = st.columns(4)
    cols[0].metric("直近判定", latest["status"])
    cols[1].metric("直近スコア", f"{latest['score_pct']:.1f}%")
    cols[2].metric("要修正日", int((quality_df["status"] == "要修正").sum()))
    cols[3].metric("平均スコア", f"{quality_df['score_pct'].mean():.1f}%")

    chart_df = quality_df.sort_values("date")
    if len(chart_df) >= 2:
        fig = px.line(
            chart_df,
            x="date",
            y="score_pct",
            markers=True,
            title="AIレポート品質スコア推移",
        )
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    display_df = quality_df.rename(columns={
        "date": "日付",
        "status": "判定",
        "score_pct": "スコア",
        "high_count": "重大",
        "medium_count": "要確認",
        "failed_count": "未通過",
        "passed_count": "通過",
        "total_checks": "全項目",
        "model_used": "モデル",
        "generated_at": "生成日時",
    })
    st.dataframe(display_df, hide_index=True, use_container_width=True)
    st.download_button(
        "品質履歴CSVをダウンロード",
        data=display_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="ai_report_quality_history.csv",
        mime="text/csv",
        use_container_width=True,
    )


def _build_report_quality_history_rows(report_dates: list[str]) -> list[dict]:
    calc = RatioCalculator()
    rows = []
    for report_date in report_dates:
        report = get_ai_report(report_date)
        if report is None:
            continue

        today_summary = calc.get_today_summary(report_date) or {}
        rows.append(_build_report_quality_row_from_markdown(
            report_date=report_date,
            markdown=report.report_markdown,
            report_json=getattr(report, "report_json", "") or "",
            today_summary=today_summary,
            model_used=getattr(report, "model_used", "") or "",
            generated_at=getattr(report, "generated_at", None),
        ))
    return rows


def _build_report_quality_row_from_markdown(
    report_date: str,
    markdown: str,
    report_json: str,
    today_summary: dict,
    model_used: str = "",
    generated_at=None,
) -> dict:
    theme_transition_context = build_theme_transition_context_for_prompt(
        target_date=report_date,
        today_summary=today_summary,
    )
    return build_quality_history_row(
        report_date=report_date,
        markdown=markdown,
        report_json=report_json,
        theme_transition_context=theme_transition_context,
        model_used=model_used,
        generated_at=generated_at,
    )


def _comparison_value(value, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def _comparison_delta(value, suffix: str = "") -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:+.1f}{suffix}"
    return f"{int(value):+d}{suffix}"


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


def _show_fetch_availability(result: dict) -> None:
    message = (
        f"{result.get('target_date')} / {result.get('status')} / "
        f"業種 {result.get('sector_count', 0)}件 / "
        f"市場全体 {'あり' if result.get('market_available') else 'なし'}"
    )
    if result.get("can_fetch"):
        st.success(message)
    elif result.get("partial"):
        st.warning(message)
    else:
        st.info(message)

    if result.get("saved_in_db"):
        st.caption("DB保存済みの日付です。")
    st.caption(result.get("message", ""))
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


# ==================================================================
# 米国ショートフロー（US-P2）
#
# 日本側の描画には一切干渉しない。データ未投入でもこのタブ内で
# 案内を出して終わる（アプリ全体を止めない）。
# ==================================================================

@st.cache_data(ttl=600, show_spinner=False)
def _cached_us_flow_frames():
    """米国ショートボリュームと日足を10分キャッシュで読む。"""
    return get_us_short_volume_df(), get_us_market_daily_df()


@st.cache_data(ttl=600, show_spinner="米国フロー指標を計算中…")
def _cached_us_report(target_date: str):
    """日付ごとにレポートをキャッシュする。

    Streamlit はウィジェット操作のたびにスクリプト全体を再実行するため、
    キャッシュしないと銘柄を切り替えるだけで毎回フル計算が走る（実測2.3秒）。
    """
    short_df, price_df = _cached_us_flow_frames()
    return build_daily_report(target_date, short_df, price_df)


def _render_us_flow_tab() -> None:
    """米国個別株のショートフロー（FINRA報告分）を表示する。"""
    st.subheader("🇺🇸 米国ショートフロー（FINRA CNMS）")
    st.caption(
        "半導体・AIインフラ・メモリの個別銘柄＋SMH/SOXX/DRAM/QQQ/SPY の日次ショートボリューム。"
        "毎営業日 08:37 JST に自動取得します。"
    )

    try:
        short_df, price_df = _cached_us_flow_frames()
    except Exception as e:  # noqa: BLE001 米国データの不調で日本側の画面を巻き込まない
        st.warning(f"米国データの読み込みに失敗しました: {e}")
        return

    if short_df is None or short_df.empty:
        st.info(
            "米国ショートフローのデータがまだありません。\n\n"
            "初回は `python -m scripts.backfill_us_short_flow --days 250` で"
            "履歴を投入してください（Zスコアの算出に過去60営業日が必要です）。"
        )
        return

    dates = sorted(short_df["date"].unique(), reverse=True)
    selected = st.selectbox("対象営業日", dates, index=0, key="us_flow_date")

    report = _cached_us_report(selected)
    coverage = report["coverage"]

    # --- この日の読み方（日本語） ---
    st.markdown("#### この日の読み方")
    st.info(report.get("summary_ja", ""))
    if coverage["missing"]:
        st.warning(
            f"この日は {coverage['present']} / {coverage['expected']} 銘柄しか取得できていません。"
            f"未取得: {', '.join(coverage['missing'])}"
        )
    st.caption(
        f"取得銘柄 {coverage['present']} / {coverage['expected']}"
        "　｜　FINRA報告分（取引所外）のみで米国市場全体ではありません。"
        "日次の数字は売買の流れ（フロー）であり、空売り残高ではありません。"
    )

    # --- バスケット ---
    if report["baskets"]:
        st.markdown("#### バスケット（銘柄をまとめた比率・出来高で加重）")
        cols = st.columns(len(report["baskets"]))
        for col, b in zip(cols, report["baskets"]):
            z20 = "N/A" if b["z20"] is None else f"{b['z20']:+.2f}"
            dod = None if b["dod_change"] is None else f"{b['dod_change']:+.2f}pt"
            col.metric(
                label=f"{b['basket']}（20日Zスコア {z20}）",
                value=f"{b['ratio']:.2f}%",
                delta=dod,
            )

    # --- ETF乖離 ---
    if report["divergences"]:
        st.markdown("#### ETFと個別銘柄の差（テーマ全体のヘッジか、銘柄選別か）")
        for d in report["divergences"]:
            div = "N/A" if d["divergence"] is None else f"{d['divergence']:+.2f}"
            st.markdown(f"- **{d['etf']}** 乖離 `{div}` … {d['interpretation']}")

    # --- ペア比較（ロング候補 vs ショート候補） ---
    _render_us_basket_pairs(report)

    # --- 今日の比較（横棒） ---
    _render_us_today_comparison(report)

    # --- アラート ---
    st.markdown(f"#### 注目銘柄（過去20日から±2σ以上ずれた銘柄）")
    descriptions = report.get("alert_descriptions") or []
    if not descriptions:
        st.info("該当なし。")
    else:
        for text in descriptions:
            st.markdown(f"- {text}")
        st.dataframe(
            _us_display_frame(pd.DataFrame(report["alerts"])),
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("全銘柄を表示", expanded=False):
        st.dataframe(
            _us_display_frame(report["metrics"]),
            use_container_width=True,
            hide_index=True,
        )

    # --- 比較チャート ---
    _render_us_comparison_chart(short_df)
    _render_us_ticker_detail(short_df, price_df)

    with st.expander("用語の説明", expanded=False):
        st.markdown(
            "- **ショート比率**: その日にFINRA報告分の出来高のうち、空売りとして報告された割合。\n"
            "- **20日Zスコア**: その銘柄自身の直近20営業日の平均から、今日が何σ離れているか。"
            "銘柄ごとに平常の水準が違う（SPYは常時65%前後、SMHは40%前後）ため、"
            "「50%を超えたから弱気」といった絶対値の判断はしません。\n"
            "- **60日順位%**: 直近60営業日の分布のなかで今日が下から何%の位置にあるか。100%に近いほど高水準。\n"
            "- **終値位置**: その日の値幅の中で終値がどこで引けたか。+1が高値引け、−1が安値引け。\n"
            "- **出来高比**: 直近20営業日の平均出来高に対する当日の倍率。\n"
            "- **乖離**: ETFのZスコア − 構成銘柄をまとめたZスコア。"
            "プラスが大きいとETF側だけ売られている（テーマ全体のヘッジ）、"
            "マイナスが大きいと個別銘柄だけ売られている（銘柄選別）と読みます。\n"
            "- **パターン候補**: 上の指標の組み合わせによる分類。"
            "いずれも当日の売買の流れから見た候補であって、断定ではありません。"
        )

    with st.expander("レポート全文（Markdown）", expanded=False):
        st.markdown(report["markdown"])


def _render_us_basket_pairs(report: dict) -> None:
    """ロング候補とショート候補のZスコア差を並べる。"""
    spreads = report.get("spreads") or []
    if not spreads:
        return

    st.markdown("#### ペア比較（ロング候補 vs ショート候補）")
    st.caption(
        "空売り比率そのものの引き算ではなく、各群が自分の過去分布からどれだけ離れたか（Zスコア）の差です。"
        "プラスが大きいほど、ショート候補側に売りが偏っています。"
    )

    rows = []
    for sp in spreads:
        rows.append({
            "対": sp["name"],
            "ロング候補": sp["long_basket"],
            "ロング側z20": "N/A" if sp["long_z20"] is None else f"{sp['long_z20']:+.2f}",
            "ショート候補": sp["short_basket"],
            "ショート側z20": "N/A" if sp["short_z20"] is None else f"{sp['short_z20']:+.2f}",
            "差": "N/A" if sp["spread"] is None else f"{sp['spread']:+.2f}",
            "読み": sp["interpretation"],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    notable = [s for s in spreads if s["spread"] is not None and abs(s["spread"]) > 1.5]
    for sp in notable:
        st.markdown(f"- **{sp['name']}**（差 {sp['spread']:+.2f}）… {sp['note']}")


def _render_us_today_comparison(report: dict) -> None:
    """対象日の全銘柄を20日Zスコアの横棒で比較する。"""
    metrics = report.get("metrics")
    if metrics is None or metrics.empty or "z20" not in metrics.columns:
        return

    frame = metrics.dropna(subset=["z20"]).copy()
    if frame.empty:
        st.caption("Zスコアを算出できる銘柄がまだありません（履歴不足）。")
        return

    frame["グループ"] = frame["ticker"].map(TICKER_GROUP).fillna("その他")
    frame["日本語名"] = frame["ticker"].map(japanese_name)
    frame["AI種別"] = frame["ticker"].map(ai_category)
    groups = sorted(frame["グループ"].unique())
    chosen = st.multiselect(
        "表示するグループ（未選択なら全部）", groups, default=[], key="us_today_groups"
    )
    if chosen:
        frame = frame[frame["グループ"].isin(chosen)]
    if frame.empty:
        st.info("選択したグループに該当する銘柄がありません。")
        return

    frame = frame.sort_values("z20")
    frame["読み"] = frame["pattern"].map(PATTERN_LABELS).fillna("")

    st.markdown("#### 銘柄比較（対象日・20日Zスコア）")
    st.caption(
        "右に伸びるほど、その銘柄にとって普段より空売りが多かった日。"
        "左に伸びるほど普段より少なかった日です。"
    )
    fig = px.bar(
        frame,
        x="z20",
        y="ticker",
        orientation="h",
        color="読み",
        hover_data={
            "日本語名": True, "AI種別": True,
            "short_ratio_pct": ":.2f", "z20": ":.2f", "読み": True,
        },
        labels={"z20": "20日Zスコア", "ticker": "銘柄", "short_ratio_pct": "ショート比率%"},
    )
    for line in (-2.0, 2.0):
        fig.add_vline(x=line, line_dash="dot", line_color="#c0392b", opacity=0.6)
    fig.update_layout(
        height=max(360, 18 * len(frame)),
        margin=dict(l=10, r=10, t=10, b=10),
        legend_title_text="パターン候補",
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_us_comparison_chart(short_df: pd.DataFrame) -> None:
    """複数銘柄のショート比率を重ねて比較する。"""
    st.markdown("#### 銘柄比較（推移）")
    st.caption("複数銘柄のショート比率を重ねて、どこにショートが偏っているかを見ます。")

    tickers = sorted(short_df["ticker"].unique())
    defaults = [t for t in ("NVDA", "AMD", "SMH") if t in tickers] or tickers[:3]
    selected = st.multiselect(
        "比較する銘柄（複数選択可）", tickers, default=defaults, key="us_compare_tickers"
    )
    period = st.radio(
        "期間", ["60営業日", "120営業日", "全期間"], index=0,
        horizontal=True, key="us_compare_period",
    )

    if not selected:
        st.info("銘柄を1つ以上選んでください。")
        return

    frame = short_df[short_df["ticker"].isin(selected)].sort_values("date")
    if period != "全期間":
        days = int(period.replace("営業日", ""))
        keep_dates = sorted(short_df["date"].unique())[-days:]
        frame = frame[frame["date"].isin(keep_dates)]

    fig = px.line(
        frame,
        x="date",
        y="short_ratio_pct",
        color="ticker",
        labels={"date": "日付", "short_ratio_pct": "ショート比率(%)", "ticker": "銘柄"},
    )
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), legend_title_text="銘柄")
    st.plotly_chart(fig, use_container_width=True)


def _render_us_ticker_detail(short_df: pd.DataFrame, price_df: Optional[pd.DataFrame]) -> None:
    """1銘柄について株価とショート比率を重ね、過去の平常域も示す。"""
    import plotly.graph_objects as go

    st.markdown("#### 株価とショート比率の重ね合わせ")
    st.caption(
        "同じ時間軸で株価（左軸）とショート比率（右軸）を並べます。"
        "帯は直近20営業日の平均±2σで、ここを外れた日が「普段と違う日」です。"
    )

    tickers = sorted(short_df["ticker"].unique())
    default_index = tickers.index("NVDA") if "NVDA" in tickers else 0
    ticker = st.selectbox("銘柄", tickers, index=default_index, key="us_flow_ticker")

    history = short_df[short_df["ticker"] == ticker][["date", "short_ratio_pct"]].sort_values("date")
    if history.empty:
        st.info("推移データがありません。")
        return

    # 平常域（直近20営業日の平均±2σ）。当日を含めないよう1日ずらす
    rolling = history["short_ratio_pct"].shift(1).rolling(20, min_periods=16)
    history = history.assign(
        mean20=rolling.mean(),
        upper=rolling.mean() + 2 * rolling.std(ddof=0),
        lower=rolling.mean() - 2 * rolling.std(ddof=0),
    )

    fig = go.Figure()

    if price_df is not None and not price_df.empty:
        price = price_df[price_df["ticker"] == ticker][["date", "close"]].sort_values("date")
        if not price.empty:
            fig.add_trace(go.Scatter(
                x=price["date"], y=price["close"], name="株価(終値)",
                yaxis="y", line=dict(color="#1f5fa8", width=2),
            ))

    fig.add_trace(go.Scatter(
        x=history["date"], y=history["upper"], name="平常域の上限(+2σ)",
        yaxis="y2", line=dict(color="rgba(192,57,43,0.25)", width=1), showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=history["date"], y=history["lower"], name="平常域(20日平均±2σ)",
        yaxis="y2", line=dict(color="rgba(192,57,43,0.25)", width=1),
        fill="tonexty", fillcolor="rgba(192,57,43,0.08)",
    ))
    fig.add_trace(go.Scatter(
        x=history["date"], y=history["short_ratio_pct"], name="ショート比率(%)",
        yaxis="y2", line=dict(color="#c0392b", width=2),
    ))

    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(title="日付"),
        yaxis=dict(title="株価(USD)", side="left"),
        yaxis2=dict(title="ショート比率(%)", side="right", overlaying="y", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def _us_display_frame(df: pd.DataFrame) -> pd.DataFrame:
    """表示用に列を絞って日本語見出しへ変換する。"""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    if "pattern" in df.columns:
        df["pattern_ja"] = df["pattern"].map(PATTERN_LABELS).fillna(df["pattern"])
    if "ticker" in df.columns:
        df["name_ja"] = df["ticker"].map(japanese_name)
        df["ai_category"] = df["ticker"].map(ai_category)

    columns = [
        ("ticker", "銘柄"),
        ("name_ja", "日本語名"),
        ("ai_category", "AI種別"),
        ("short_ratio_pct", "ショート比率%"),
        ("z20", "20日Zスコア"),
        ("z60", "60日Zスコア"),
        ("pct60", "60日順位%"),
        ("daily_return", "騰落率"),
        ("clv", "終値位置"),
        ("volume_ratio", "出来高比"),
        ("pattern_ja", "パターン候補"),
    ]
    available = [(src, label) for src, label in columns if src in df.columns]
    view = df[[src for src, _ in available]].rename(dict(available), axis=1)

    if "騰落率" in view.columns:
        view["騰落率"] = view["騰落率"].map(
            lambda v: "N/A" if pd.isna(v) else f"{v * 100:+.2f}%"
        )
    for column in ["ショート比率%", "20日Zスコア", "60日Zスコア", "60日順位%", "終値位置", "出来高比"]:
        if column in view.columns:
            view[column] = view[column].map(
                lambda v: "N/A" if pd.isna(v) else f"{v:.2f}"
            )
    return view

if __name__ == "__main__":
    main()
