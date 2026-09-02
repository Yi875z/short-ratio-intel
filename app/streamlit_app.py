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
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    GEMINI_MODEL,
    GEMINI_MODEL_DEFAULT,
    GEMINI_MODEL_IS_OVERRIDDEN,
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
from src.macro_context.market_quotes import (
    fetch_nikkei_ohlc_history,
    fetch_nikkei_topix_close_history,
    fetch_nt_ratio_history,
    fetch_quotes,
)
from src.macro_context.sector_price import returns_by_sector_code
from src.analyzer.sector_insight import HIGH_ZONE_MIN_RATIO, build_sector_insights
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
# 需給モニター。空売り比率・絶対額・流動性・価格反応を突き合わせてレジームを判定する。
from src.analyzer.market_breadth import DEFAULT_BREADTH_SCOPE
from src.analyzer.pressure_metrics import (
    build_pressure_metrics,
    format_pct as _fmt_pct,
    format_signed_pct as _fmt_signed_pct,
    format_trillion_yen as _fmt_trillion,
    to_trillion_yen,
)
from src.analyzer.pressure_regime import PressureRegimeClassifier
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
    get_market_breadth_df,
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
from src.storage.db import (
    get_us_market_daily_df,
    get_us_short_interest_df,
    get_us_short_volume_df,
)
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
    st.caption(
        "日本の業種別空売り比率と、米国AI関連株のショートフローを一画面で確認します。"
        "日本は東証33業種の日次空売り比率とJPX内訳、市場テーマ判定、Gemini AIレポート。"
        "米国はFINRA報告分の日次ショートボリュームを、銘柄自身の過去分布と比べて評価します。"
    )

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
        pressure_tab,
        sectors_tab,
        breakdown_tab,
        theme_tab,
        market_data_tab,
        calendar_tab,
        report_tab,
        history_tab,
        us_flow_tab,
    ) = st.tabs(
        ["概要", "⚖️ 需給モニター", "業種", "JPX内訳", "市場テーマ", "🌐 市場データ",
         "📅 カレンダー", "AIレポート", "履歴", "🇺🇸 米国ショート"]
    )

    with overview_tab:
        _render_overview(selected_date, today_summary, market_trend_df, anomalies)
    with pressure_tab:
        _render_pressure_monitor(selected_date, market_trend_df)
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


@st.cache_data(ttl=300, show_spinner=False)
def _cached_index_close_history(from_iso: str, to_iso: str):
    """日経平均・TOPIXの終値時系列を5分キャッシュで取得する。"""
    return fetch_nikkei_topix_close_history(from_iso, to_iso)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_nikkei_ohlc(from_iso: str, to_iso: str):
    """日経平均の日足OHLCを5分キャッシュで取得する（ロウソク足を選んだときだけ呼ぶ）。"""
    return fetch_nikkei_ohlc_history(from_iso, to_iso)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_sector_returns(target_date: str):
    """業種別株価指数の前日騰落率を10分キャッシュで取得する（S33コード→騰落率）。"""
    return returns_by_sector_code(target_date)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_sector_history(target_date: str, days: int = 90):
    """Zスコア・パーセンタイル・連続日数に使う長めの業種履歴。

    画面が使う weekly_df（14日）ではサンプルが足りないため別に読む。
    既存の weekly_df の流れは変えない。
    """
    return RatioCalculator().get_weekly_trend(target_date, days=days)


_LINE_STYLE = "ライン"
_CANDLE_STYLE = "ロウソク足"

# 日経平均に重ねる指数平滑移動平均線。表示するかどうかは画面で選ぶ。
_EMA_PERIODS = (20, 60, 120)
_EMA_COLORS = {20: "#2980b9", 60: "#8e44ad", 120: "#7f8c8d"}

# EMAの助走期間（カレンダー日）。表示範囲のデータだけで計算すると窓の先頭が不正確になる。
# 600日＝約400営業日あり、最長のEMA120（span=120）に対して3倍以上の助走が取れる。
# 助走ぶんは計算に使うだけでチャートには出さない（clip_to_window で切る）。
_EMA_WARMUP_DAYS = 600


def add_ema_columns(df, close_col: str = "close", periods=_EMA_PERIODS):
    """終値からEMAを計算して ema{期間} 列を足す。

    ⚠️ 表示範囲より前の助走データを含んだ状態で呼ぶこと。表示範囲だけで計算すると
    窓の先頭のEMAが初期値に引きずられ、実際とは違う線になる。
    """
    if df is None or len(df) == 0 or not periods:
        return df

    out = df.copy()
    for period in periods:
        out[f"ema{period}"] = out[close_col].ewm(span=period, adjust=False).mean()
    return out


def clip_to_window(df, from_iso: str, to_iso: str):
    """EMAを計算し終えたあとで表示範囲へ切る（助走期間をチャートに出さない）。"""
    if df is None or len(df) == 0:
        return df

    mask = (df["date"] >= pd.to_datetime(from_iso)) & (df["date"] <= pd.to_datetime(to_iso))
    return df[mask]


def _apply_index_axes(fig: go.Figure, title: str, date_range) -> None:
    """上の空売りチャートと横軸を揃え、非営業日の空白を潰す。"""
    fig.update_layout(
        title=title,
        height=300,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
    )
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    if date_range:
        start, end = (pd.to_datetime(value) for value in date_range)
        # ロウソク足1本は前後0.5日ぶんの幅を持つ。範囲を終端ぴったりにすると
        # 最新の足が半分だけ描かれて切れるため、両端に余白を入れる。
        pad = pd.Timedelta(hours=18)
        fig.update_xaxes(range=[start - pad, end + pad])


def build_nikkei_figure(
    close_df, ohlc_df, style: str, date_range=None, ema_periods=()
) -> go.Figure:
    """日経平均のチャート。

    ロウソク足は四本値が揃っているときだけ描き、取得できなければラインへ落とす
    （欠けた足を描いて嘘のチャートにしないため）。EMAは実際に描画した系列から引く。
    """
    fig = go.Figure()
    use_candle = style == _CANDLE_STYLE and ohlc_df is not None and not ohlc_df.empty
    source = None

    if use_candle:
        fig.add_trace(go.Candlestick(
            x=ohlc_df["date"],
            open=ohlc_df["open"],
            high=ohlc_df["high"],
            low=ohlc_df["low"],
            close=ohlc_df["close"],
            name="日経平均",
        ))
        fig.update_layout(xaxis_rangeslider_visible=False)
        source = ohlc_df
    elif close_df is not None and not close_df.empty:
        fig.add_trace(go.Scatter(
            x=close_df["date"], y=close_df["nikkei"], mode="lines", name="日経平均",
        ))
        source = close_df

    drawn_ema = []
    if source is not None:
        for period in ema_periods:
            column = f"ema{period}"
            if column not in source.columns:
                continue
            fig.add_trace(go.Scatter(
                x=source["date"],
                y=source[column],
                mode="lines",
                name=f"EMA{period}",
                line=dict(width=1.4, color=_EMA_COLORS.get(period)),
            ))
            drawn_ema.append(period)

    _apply_index_axes(fig, "日経平均", date_range)
    if drawn_ema:
        # EMAを重ねたときだけ凡例を出す（どの線が何期間か分からないと読めないため）
        fig.update_layout(
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        )
    return fig


def build_topix_figure(close_df, date_range=None) -> go.Figure:
    """TOPIXのチャート。公開データに四本値が無いため常にライン（代理ETFで代用しない）。"""
    fig = go.Figure()
    if close_df is not None and not close_df.empty:
        fig.add_trace(go.Scatter(
            x=close_df["date"],
            y=close_df["topix"],
            mode="lines",
            name="TOPIX",
            line=dict(color="#e67e22"),
        ))
    _apply_index_axes(fig, "TOPIX", date_range)
    return fig


def _render_index_charts(date_min, date_max, key_prefix: str) -> None:
    """空売りチャートの直下に、同じ日付範囲の日経平均・TOPIXを並べる。

    外部サイトからの取得なので、失敗しても caption を出すだけにする（fail-soft）。
    上の空売りチャートを巻き込んで壊さないことを優先する。
    """
    if date_min is None or date_max is None:
        return

    from_iso = str(pd.to_datetime(date_min).date())
    to_iso = str(pd.to_datetime(date_max).date())
    # EMAの助走ぶんまで遡って取得し、計算し終えてから表示範囲へ切る
    warmup_iso = str((pd.to_datetime(from_iso) - pd.Timedelta(days=_EMA_WARMUP_DAYS)).date())

    close_full = _cached_index_close_history(warmup_iso, to_iso)
    if close_full is None or close_full.empty:
        st.caption("日経平均・TOPIXの値動きを取得できませんでした（時間をおいて再試行してください）。")
        return

    style_col, ema_col = st.columns([1, 2])
    with style_col:
        style = st.radio(
            "表示形式",
            [_LINE_STYLE, _CANDLE_STYLE],
            horizontal=True,
            key=f"index_style_{key_prefix}",
        )
    with ema_col:
        ema_periods = st.multiselect(
            "日経平均に重ねるEMA",
            list(_EMA_PERIODS),
            default=list(_EMA_PERIODS),
            format_func=lambda period: f"EMA{period}",
            key=f"index_ema_{key_prefix}",
        )

    ohlc_full = _cached_nikkei_ohlc(warmup_iso, to_iso) if style == _CANDLE_STYLE else None
    if style == _CANDLE_STYLE and (ohlc_full is None or ohlc_full.empty):
        st.caption("日経平均の四本値を取得できなかったため、ラインで表示します。")

    close_df = clip_to_window(
        add_ema_columns(close_full, "nikkei", ema_periods), from_iso, to_iso
    )
    ohlc_df = clip_to_window(
        add_ema_columns(ohlc_full, "close", ema_periods), from_iso, to_iso
    ) if ohlc_full is not None else None

    date_range = (from_iso, to_iso)
    st.plotly_chart(
        build_nikkei_figure(close_df, ohlc_df, style, date_range, ema_periods),
        use_container_width=True,
    )
    st.plotly_chart(build_topix_figure(close_df, date_range), use_container_width=True)
    st.caption(
        "上の空売り比率と同じ期間で表示しています。EMAは表示期間より前の助走データを含めて"
        "計算しています。TOPIXは公開データに始値・高値・安値が無いためライン表示のみ"
        "（出所: nikkei225jp.com／日経平均の四本値は Yahoo ^N225）。"
    )


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
                    [{"銘柄": q.label, "ティッカー": q.ticker, "エラー": q.error} for q in quotes]
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
    # 対象日リストをスクレイパー任せにすると、先方のHTML変更で候補が空になった瞬間に
    # 生きている JPX 公式PDF まで一度も参照されず、静かに全欠測になる（2026-08 に3営業日欠測）。
    # 公式PDFの公開日と突き合わせ、どちらか片方が生きていれば取得を継続できるようにする。
    candidate_dates = sorted(
        {record["Date"] for record in fallback_sector_records}
        | set(jpx.get_available_dates(days)),
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


# ==================================================================
# 需給モニター
#
# 空売り比率だけでなく、絶対額（代金）・市場流動性・価格反応を突き合わせて
# 売り圧力を判定する。比率と絶対額は別ブロックに分けて表示し、
# 対象範囲の違うもの（騰落銘柄数は市場区分別、空売り集計は東証全体）は
# 同じ数式に入れず、並べて示すだけに留める。
# ==================================================================

_REGIME_COLORS = {
    "SELL_PRESSURE": "#c0392b",
    "THIN_MARKET": "#8e7cc3",
    "ABSORPTION": "#1e8449",
    "BROAD_DE-RISKING": "#7b241c",
    "SHORT_COVER_CANDIDATE": "#1f6fb2",
    "NEUTRAL": "#7f8c8d",
}

_CONFIDENCE_LABELS = {
    "high": "高", "medium": "中", "low": "低", "n/a": "判定不能",
}

_PRESSURE_HISTORY_DAYS = 20


@st.cache_data(ttl=600)
def _cached_breadth_frame(target_date: str) -> pd.DataFrame:
    return get_market_breadth_df(date=target_date)


def _render_pressure_monitor(selected_date: str, market_trend_df: pd.DataFrame) -> None:
    st.subheader("⚖️ 需給モニター")
    st.caption(
        "空売り比率・空売り代金・市場売買代金・価格反応を突き合わせて売り圧力を判定します。"
        "比率と絶対額は別々に表示します（比率が同じでも商いが半分なら実額は半分です）。"
    )

    breadth_frame = _cached_breadth_frame(selected_date)
    breadth_row, scope_label = _select_breadth_scope(breadth_frame)

    metrics = build_pressure_metrics(selected_date, market_trend_df, breadth_row)
    if metrics.ratios.total_short_pct is None and metrics.values.market_volume_va is None:
        st.warning(
            f"{selected_date} の空売り集計データがありません。"
            "左メニューからデータ取得を実行してください。"
        )
        return

    _render_pressure_ratio_row(metrics)
    _render_pressure_value_row(metrics)
    _render_pressure_price_row(metrics, scope_label)

    st.divider()
    _render_regime_panel(metrics)

    st.divider()
    _render_pressure_history_chart(selected_date, market_trend_df)


def _select_breadth_scope(breadth_frame: pd.DataFrame):
    """騰落銘柄数の市場区分を選ばせる。無ければ (None, None) を返す。"""
    if breadth_frame is None or breadth_frame.empty:
        return None, None

    scopes = list(breadth_frame["market_scope"])
    default_index = scopes.index(DEFAULT_BREADTH_SCOPE) if DEFAULT_BREADTH_SCOPE in scopes else 0
    labels = {
        row["market_scope"]: row["scope_label"] or row["market_scope"]
        for _, row in breadth_frame.iterrows()
    }
    chosen = st.selectbox(
        "騰落銘柄数の対象市場",
        scopes,
        index=default_index,
        format_func=lambda scope: labels.get(scope, scope),
        help=(
            "空売り集計は東証全体（外国株券等を含む）が対象で、この騰落銘柄数とは"
            "母集団が異なります。両者を割り算せず、並べて読んでください。"
        ),
        key="pressure_breadth_scope",
    )
    row = breadth_frame[breadth_frame["market_scope"] == chosen].iloc[0].to_dict()
    return row, labels.get(chosen, chosen)


def _render_pressure_ratio_row(metrics) -> None:
    st.markdown("##### ① 比率（分母＝合計売買代金）")
    ratios = metrics.ratios
    cols = st.columns(5)
    cols[0].metric("空売り比率", _fmt_pct(ratios.total_short_pct),
                   _pt(_regime_ratio_dod_pt(metrics)))
    cols[1].metric("価格規制あり比率", _fmt_pct(ratios.with_restriction_pct))
    cols[2].metric("価格規制なし比率", _fmt_pct(ratios.without_restriction_pct))
    cols[3].metric("実注文比率", _fmt_pct(ratios.actual_order_pct))
    cols[4].metric(
        "規制なし構成比", _fmt_pct(ratios.without_share_pct),
        help="分母は総空売り代金。高いほど裁定・ヘッジ由来の可能性があり、弱気と断定できません。",
    )


def _render_pressure_value_row(metrics) -> None:
    st.markdown("##### ② 代金（絶対額・単位は兆円）")
    values = metrics.values
    short_change = metrics.short_value_change
    volume_change = metrics.market_volume_change

    cols = st.columns(4)
    cols[0].metric(
        "総空売り代金", _fmt_trillion(values.total_short_va),
        _fmt_signed_pct(short_change.dod_pct, 1),
        help="デルタは前営業日比。5日平均比は下段に表示します。",
    )
    cols[1].metric("価格規制あり代金", _fmt_trillion(values.with_restriction_va))
    cols[2].metric("価格規制なし代金", _fmt_trillion(values.without_restriction_va))
    cols[3].metric(
        "市場売買代金", _fmt_trillion(values.market_volume_va),
        _fmt_signed_pct(volume_change.dod_pct, 1),
    )

    sub = st.columns(4)
    sub[0].caption(f"空売り代金 5日平均比: {_fmt_signed_pct(short_change.vs_avg_pct, 1)}")
    sub[1].caption(f"空売り代金 Zスコア: {_z_text(short_change)}")
    sub[2].caption(f"売買代金 5日平均比: {_fmt_signed_pct(volume_change.vs_avg_pct, 1)}")
    sub[3].caption(f"売買代金 Zスコア: {_z_text(volume_change)}")


def _render_pressure_price_row(metrics, scope_label: str | None) -> None:
    st.markdown("##### ③ 価格と市場の広がり")
    price = metrics.price
    breadth = metrics.breadth

    cols = st.columns(4)
    cols[0].metric(
        "TOPIX終値",
        f"{price.topix_close:,.2f}" if price.topix_close is not None else "—",
        _fmt_signed_pct(price.topix_change_pct, 2),
    )
    cols[1].metric(
        f"値上がり銘柄数（{scope_label or '—'}）",
        f"{breadth.advancing:,}" if breadth.advancing is not None else "—",
    )
    cols[2].metric(
        f"値下がり銘柄数（{scope_label or '—'}）",
        f"{breadth.declining:,}" if breadth.declining is not None else "—",
    )
    cols[3].metric(
        "ネットブレッドス",
        f"{breadth.net_breadth:+.3f}" if breadth.net_breadth is not None else "—",
        help="(値上がり − 値下がり) ÷ (値上がり + 値下がり)。+1に近いほど全面高。",
    )

    if metrics.missing_inputs:
        st.info(
            "未取得の入力: " + " / ".join(metrics.missing_inputs)
            + "。これらを必要とするレジームは判定していません（0とみなす補間はしません）。"
        )


def _render_regime_panel(metrics) -> None:
    result = PressureRegimeClassifier().classify(metrics)
    color = _REGIME_COLORS.get(result.primary, "#7f8c8d")
    confidence = _CONFIDENCE_LABELS.get(result.confidence, result.confidence)

    st.markdown(
        f"""
        <div style="border-left:6px solid {color};padding:0.6rem 1rem;
                    background:rgba(127,127,127,0.08);border-radius:4px;">
          <div style="font-size:1.35rem;font-weight:700;color:{color};">
            {result.primary_label}
            <span style="font-size:0.85rem;font-weight:500;color:#666;">
              （{result.primary} ／ 確信度: {confidence}）
            </span>
          </div>
          <div style="margin-top:0.35rem;font-size:0.95rem;">{result.description}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if result.reasons:
        st.markdown("**判定の根拠**")
        for reason in result.reasons:
            st.markdown(f"- {reason}")

    for caveat in result.caveats:
        st.warning(caveat)

    if result.also_matched:
        labels = ", ".join(result.also_matched)
        st.caption(f"同時に条件を満たしたレジーム: {labels}")

    with st.expander("全レジームの条件充足状況を見る"):
        for verdict in result.verdicts:
            status = "成立" if verdict.matched else ("判定不能" if verdict.missing_inputs else "不成立")
            st.markdown(f"**{verdict.label}（{verdict.regime}） — {status}**")
            for item in verdict.satisfied:
                st.markdown(f"- ✅ {item}")
            for item in verdict.unsatisfied:
                st.markdown(f"- ❌ {item}")
            if verdict.missing_inputs:
                st.markdown(f"- ⚠️ 入力不足: {' / '.join(verdict.missing_inputs)}")


def _render_pressure_history_chart(
    selected_date: str, market_trend_df: pd.DataFrame
) -> None:
    """過去20営業日の比率・空売り代金・市場売買代金を日付軸を揃えて並べる。

    比率(%)と代金(兆円)は軸の意味が違うため、二軸で重ねずに段を分ける。
    重ねると「比率が上がった＝売りが増えた」と誤読しやすい。
    """
    from plotly.subplots import make_subplots

    st.markdown(f"##### 過去{_PRESSURE_HISTORY_DAYS}営業日の推移")

    if market_trend_df is None or market_trend_df.empty:
        st.info("推移を描くデータがありません。")
        return

    history = market_trend_df.sort_values("date")
    history = history[history["date"] <= selected_date].tail(_PRESSURE_HISTORY_DAYS).copy()
    if history.empty:
        st.info("推移を描くデータがありません。")
        return

    # ⚠️ JPX内訳が取れなかった日を 0 として描かないこと。
    # stock-marketdata へフォールバックした日は内訳が 0 で保存されており、
    # そのまま描くと「空売り比率0%・空売り代金0円」という嘘のグラフになる
    # （2026-09-01 に実際に発生）。内訳の無い日は欠測として線と棒を途切れさせる。
    has_breakdown = history.apply(
        lambda row: bool(
            (row.get("total_short_va") or 0)
            or (row.get("shrt_with_res_va") or 0)
            or (row.get("shrt_no_res_va") or 0)
        ),
        axis=1,
    )

    # 空売り比率は取得元から常に得られるため、内訳の有無にかかわらず描ける。
    history["空売り比率"] = history["short_ratio_pct"]
    history["規制あり比率"] = history.apply(
        lambda row: _safe_ratio_pct(row.get("shrt_with_res_va"), row.get("total_volume_va")),
        axis=1,
    ).where(has_breakdown)
    history["空売り代金"] = history["total_short_va"].map(to_trillion_yen).where(has_breakdown)
    history["市場売買代金"] = history["total_volume_va"].map(to_trillion_yen)

    missing_days = int((~has_breakdown).sum())

    figure = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=("空売り比率・規制あり比率（%）", "空売り代金（兆円）", "市場売買代金（兆円）"),
    )
    figure.add_trace(
        go.Scatter(x=history["date"], y=history["空売り比率"], name="空売り比率",
                   mode="lines+markers", line=dict(color="#c0392b")),
        row=1, col=1,
    )
    figure.add_trace(
        go.Scatter(x=history["date"], y=history["規制あり比率"], name="規制あり比率",
                   mode="lines+markers", line=dict(color="#e67e22", dash="dot")),
        row=1, col=1,
    )
    figure.add_trace(
        go.Bar(x=history["date"], y=history["空売り代金"], name="空売り代金",
               marker_color="#8e44ad"),
        row=2, col=1,
    )
    figure.add_trace(
        go.Bar(x=history["date"], y=history["市場売買代金"], name="市場売買代金",
               marker_color="#2980b9"),
        row=3, col=1,
    )
    figure.update_yaxes(title_text="%", row=1, col=1)
    figure.update_yaxes(title_text="兆円", row=2, col=1)
    figure.update_yaxes(title_text="兆円", row=3, col=1)
    figure.update_xaxes(title_text="日付", row=3, col=1)
    figure.update_layout(
        height=720, margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(figure, use_container_width=True)
    st.caption(
        "比率(%)と代金(兆円)は軸の意味が異なるため段を分けています。"
        "比率の上昇が必ずしも空売り代金の増加を意味しない点にご注意ください。"
    )
    if missing_days:
        st.warning(
            f"表示期間のうち {missing_days}営業日は JPX公式PDF の内訳が未取得です"
            "（規制あり比率と空売り代金が途切れています）。"
            "空売り比率と市場売買代金は取得できています。"
            "左メニューの「指定日を取得」で該当日を取り直すと内訳が埋まります。"
        )


def _safe_ratio_pct(numerator, denominator):
    try:
        numerator = float(numerator)
        denominator = float(denominator)
    except (TypeError, ValueError):
        return None
    if not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def _regime_ratio_dod_pt(metrics):
    """空売り比率の前日比を pt で返す（比率の変化は pt で読むのが実務）。"""
    change = metrics.total_ratio_change
    if change.latest is None or change.dod_pct is None or change.dod_pct == -100:
        return None
    previous = change.latest / (1 + change.dod_pct / 100)
    return round(change.latest - previous, 2)


def _z_text(change) -> str:
    if change.zscore is None:
        return "—（サンプル不足）"
    return f"{change.zscore:+.2f}（n={change.sample_size}）"


def _render_overview(
    selected_date: str,
    today_summary: dict,
    market_trend_df: pd.DataFrame,
    anomalies: list,
) -> None:
    # どの営業日の数字を見ているかを画面上で明示する。
    # サイドバーの「分析日」だけだと、タブを切り替えた後に見落としやすい。
    st.markdown(f"##### 分析日: {_ja_date_label(selected_date)}")
    st.caption(
        f"この画面の指標・チャートはすべて {selected_date}（JPXの集計対象日）が基準です。"
        "左メニューの「分析日」で切り替えられます。"
    )

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
            labels=_COLUMN_LABELS,
            title="東証全体 空売り比率推移",
        )
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)
        _render_index_charts(trend["date"].min(), trend["date"].max(), "overview")

    high_col, low_col = st.columns(2)
    with high_col:
        st.subheader("高空売り 上位5業種")
        st.dataframe(_ja_frame(_sector_frame(today_summary.get("top5_high", []))), hide_index=True)
    with low_col:
        st.subheader("低空売り 下位5業種")
        st.dataframe(_ja_frame(_sector_frame(today_summary.get("top5_low", []))), hide_index=True)

    if anomalies:
        st.subheader("異常値")
        st.dataframe(_ja_frame(pd.DataFrame([a.__dict__ for a in anomalies])), hide_index=True)


# 画面に出す表の見出しは日本語で統一する。DBやデータクラスの列名は英語のままなので、
# 表示の直前にここで置き換える（st.dataframe に渡す前に必ず _ja_frame を通すこと）。
_COLUMN_LABELS = {
    # 業種・空売り比率
    "sector_name": "業種",
    "s33_code": "業種コード",
    "short_ratio_pct": "空売り比率(%)",
    "current_ratio": "空売り比率(%)",
    "dod_change": "前日比(pt)",
    "zone_label": "ゾーン",
    "zone_key": "ゾーン区分",
    "change_pct": "株価騰落率(%)",
    "quadrant": "象限",
    "zscore": "Zスコア",
    "percentile": "パーセンタイル",
    "streak_days": "連続日数",
    "with_ratio": "規制あり比率(%)",
    "without_ratio": "規制なし比率(%)",
    "without_share": "規制なし構成比(%)",
    "shrt_with_res_va": "規制あり売買代金",
    "shrt_no_res_va": "規制なし売買代金",
    "total_short_va": "空売り代金",
    "sell_ex_short_va": "実売り代金",
    "total_volume_va": "総売買代金",
    # 異常値
    "event_type": "種別",
    "value": "値",
    "severity": "重要度",
    "description": "内容",
    # 機械判定シグナル
    "category": "分類",
    "target": "対象",
    "signal": "シグナル",
    "rationale": "根拠",
    "watch_point": "着目点",
    "details": "詳細",
    "invalidation_condition": "否定条件",
    # 市場テーマ・ニュース
    "date": "日付",
    "key": "キー",
    "name": "テーマ",
    "score": "スコア",
    "status": "状態",
    "confidence": "確度",
    "evidence": "根拠",
    "evidence_count": "根拠数",
    "related_sectors": "関連業種",
    "unverified_count": "未検証数",
    "unverified_data": "未検証データ",
    "state": "変化",
    "current_score": "今回スコア",
    "previous_score": "前回スコア",
    "score_change": "スコア差",
    "current_status": "今回状態",
    "previous_status": "前回状態",
    "query": "検索語",
    "title": "見出し",
    "url": "URL",
    "source": "出所",
    "published_date": "公開日",
    "snippet": "抜粋",
    # AIレポート品質チェック
    "check": "項目",
    "result": "結果",
    "message": "メッセージ",
    # チャートの軸・凡例でも同じ辞書を使う
    "nt_ratio": "NT倍率",
    "score_pct": "スコア(%)",
    # 共通
    "ticker": "銘柄",
}

# 区分値そのものが英語のものも読み替える（列名 → {英語値: 日本語値}）
_VALUE_LABELS = {
    "event_type": {
        "dod_spike": "前日比の急変",
        "zscore_outlier": "統計的な外れ値",
        "absolute_extreme": "絶対値が極端",
    },
    "severity": {
        "high": "重大",
        "medium": "中程度",
        "low": "軽微",
    },
}


def _ja_frame(df: pd.DataFrame) -> pd.DataFrame:
    """表示用に英語の列名・区分値を日本語へ置き換える（未知の列はそのまま残す）。"""
    if df is None or len(df) == 0:
        return df

    out = df.copy()
    for column, mapping in _VALUE_LABELS.items():
        if column in out.columns:
            out[column] = out[column].map(lambda v: mapping.get(v, v))
    return out.rename(columns=_COLUMN_LABELS)


# 業種テーブルの表示順（見出しは _COLUMN_LABELS が正）
_SECTOR_TABLE_ORDER = (
    "sector_name",
    "short_ratio_pct",
    "dod_change",
    "change_pct",
    "quadrant",
    "zscore",
    "percentile",
    "without_share",
    "streak_days",
    "zone_label",
)


def _sector_insight_frame(today_summary: dict) -> pd.DataFrame:
    """業種の空売り比率に文脈（株価騰落率・象限・Zスコア・規制内訳・連続日数）を付けた表。

    計算は src/analyzer/sector_insight.py に集約してあり、AIレポートと同じ数字を使う。
    外部取得（業種別株価）と履歴読み込みはどちらも失敗しうるので fail-soft にする。
    """
    target_date = today_summary.get("date") or ""
    if not target_date:
        return pd.DataFrame()

    try:
        sector_returns = _cached_sector_returns(target_date)
    except Exception:
        sector_returns = {}
    try:
        history_df = _cached_sector_history(target_date)
    except Exception:
        history_df = None

    return pd.DataFrame(build_sector_insights(today_summary, history_df, sector_returns))


def _render_quadrant_scatter(insight_df: pd.DataFrame) -> None:
    """空売り比率の前日比 × 業種株価の騰落率を4象限で見る散布図。"""
    plotted = insight_df.dropna(subset=["change_pct", "dod_change"])
    missing = len(insight_df) - len(plotted)
    if plotted.empty:
        st.caption("業種別株価の騰落率を取得できなかったため、4象限マップは表示できません。")
        return

    fig = px.scatter(
        plotted,
        x="change_pct",
        y="dod_change",
        color="zone_label",
        size="short_ratio_pct",
        hover_name="sector_name",
        hover_data={"quadrant": True, "short_ratio_pct": ":.1f"},
        labels={
            "change_pct": "業種株価 騰落率(%)",
            "dod_change": "空売り比率 前日比(pt)",
            "zone_label": "ゾーン",
            "short_ratio_pct": "空売り比率(%)",
            "quadrant": "象限",
        },
        title="4象限マップ（空売り比率の前日比 × 業種株価の騰落率）",
    )
    fig.add_hline(y=0, line_width=1, line_color="#888888")
    fig.add_vline(x=0, line_width=1, line_color="#888888")
    fig.update_layout(height=520, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "右上=比率上昇×株価上昇（売り吸収・踏み上げの可能性）／"
        "左上=比率上昇×株価下落（方向性売り優勢の可能性）／"
        "右下=比率低下×株価上昇（ショートカバー主導の可能性）／"
        "左下=比率低下×株価下落（売り圧力後退でも買い不在の可能性）。"
        "いずれも可能性であり断定ではありません。"
        + (f" 株価を取得できず除外: {missing}業種。" if missing else "")
    )


def _render_streak_ranking(insight_df: pd.DataFrame) -> None:
    """高空売りゾーンが何営業日続いているかの上位5業種。

    単日 50% より「連続で警戒ゾーン」の方が踏み上げの燃料としては重い。
    テーブルの並べ替えでも見られるが、単日スパイクと持続の区別は明示的に出す。
    """
    if "streak_days" not in insight_df.columns:
        return

    threshold = f"{HIGH_ZONE_MIN_RATIO:.0f}%"
    ranked = (
        insight_df[insight_df["streak_days"] > 0]
        .sort_values("streak_days", ascending=False)
        .head(5)
    )
    st.markdown(f"##### 高空売りの連続日数 上位5業種（{threshold}以上が続いている業種）")
    if ranked.empty:
        st.caption(f"空売り比率が{threshold}以上で連続している業種はありません。")
        return

    st.dataframe(
        _ja_frame(
            ranked[["sector_name", "streak_days", "short_ratio_pct", "zone_label"]]
        ).round(2),
        hide_index=True,
        use_container_width=True,
    )


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
        labels=_COLUMN_LABELS,
        title="業種別 空売り比率",
    )
    fig.update_layout(height=720, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)

    insight_df = _sector_insight_frame(today_summary)
    if insight_df.empty:
        # 文脈を作れない場合でも、素の比率テーブルだけは必ず出す
        columns = [
            "sector_name", "short_ratio_pct", "dod_change",
            "shrt_with_res_va", "shrt_no_res_va", "total_volume_va", "zone_label",
        ]
        st.dataframe(
            _ja_frame(sector_df[[col for col in columns if col in sector_df.columns]]),
            hide_index=True,
            use_container_width=True,
        )
    else:
        _render_quadrant_scatter(insight_df)

        st.markdown("##### 業種別の空売り比率と文脈")
        display_df = _ja_frame(insight_df[
            [col for col in _SECTOR_TABLE_ORDER if col in insight_df.columns]
        ])
        st.dataframe(
            display_df.round(2),   # 数値列だけ丸まる。生の精度はデータ側に残す
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "Zスコア／パーセンタイルは、その業種自身の過去60営業日の分布に対する位置。"
            "業種ごとに空売り比率の平常水準が違う（証券業は元から高く、電気・ガス業は低い）ため、"
            "生の比率より水準判断に向く。履歴5営業日未満の業種は空欄。"
            "規制なし構成比が高いほど裁定・ヘッジ色、低いほど方向性の売り色が強い。"
        )

        _render_streak_ranking(insight_df)

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
            labels=_COLUMN_LABELS,
            title=f"{selected_sector} 空売り比率推移",
        )
        st.plotly_chart(fig, use_container_width=True)
        _render_index_charts(trend["date"].min(), trend["date"].max(), "sector")


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
    fig = px.pie(
        df, names="category", values="value",
        labels=_COLUMN_LABELS, title="JPX空売り内訳",
    )
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)

    signals = today_summary.get("flow_signals", [])
    if signals:
        st.subheader("機械判定シグナル")
        st.dataframe(_ja_frame(pd.DataFrame(signals)), hide_index=True, use_container_width=True)


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
        st.dataframe(_ja_frame(pd.DataFrame(saved_themes)), hide_index=True, use_container_width=True)

    saved_news = get_market_news_snapshots(selected_date)
    if saved_news:
        st.subheader("保存済みニュース")
        st.dataframe(_ja_frame(pd.DataFrame(saved_news)), hide_index=True, use_container_width=True)

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
                _ja_frame(pd.DataFrame(comparison_rows)),
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
                labels=_COLUMN_LABELS,
                title="市場テーマ スコア推移",
            )
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        _ja_frame(history_df.sort_values(["date", "score"], ascending=[False, False])),
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

    _render_effective_model_caption()

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
                model_used=generator.model_name,
            )
            quality_comparison = build_quality_comparison(before_quality_row, after_quality_row)
            quality_comparison_markdown = build_quality_comparison_markdown(quality_comparison)
            save_ai_report(
                selected_date,
                report_obj.current_macro_context,
                markdown,
                report_json=report_json,
                model_used=generator.model_name,
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


def _render_effective_model_caption() -> None:
    """このアプリが実際に使う Gemini モデルを表示する。

    2026-08-24 の障害では Streamlit Cloud Secrets だけ古いモデルが残っており、
    手動生成は通るのに定時実行だけ落ちる、という食い違いの発見が遅れた。
    どのモデルで生成しているかを画面に出して、その食い違いを見えるようにする。
    """
    if GEMINI_MODEL_IS_OVERRIDDEN:
        st.caption(
            f"⚠️ 使用モデル: `{GEMINI_MODEL}`（Secrets の `GEMINI_MODEL` による上書き。"
            f"リポジトリ既定は `{GEMINI_MODEL_DEFAULT}`）"
        )
    else:
        st.caption(f"使用モデル: `{GEMINI_MODEL}`（日次クォータ枯渇時は退避モデルへ自動切替）")


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
            st.dataframe(_ja_frame(pd.DataFrame(failed_rows)), hide_index=True, use_container_width=True)
        else:
            st.success("品質チェックで重大な問題は検出されませんでした。")

        show_passed = st.checkbox(
            "通過項目も表示",
            value=False,
            key=f"quality_show_passed_{stored_report.date}",
        )
        if show_passed:
            st.dataframe(
                _ja_frame(pd.DataFrame(quality.to_rows(include_passed=True))),
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
            labels=_COLUMN_LABELS,
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


_WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")


def _ja_date_label(iso_date) -> str:
    """ISO日付を「2026年9月2日（水）」にする。読めない値は原文のまま返す。"""
    try:
        d = date.fromisoformat(str(iso_date))
    except (TypeError, ValueError):
        return str(iso_date)
    return f"{d.year}年{d.month}月{d.day}日（{_WEEKDAY_JA[d.weekday()]}）"


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
    return (
        get_us_short_volume_df(),
        get_us_market_daily_df(),
        get_us_short_interest_df(latest_only=True),
    )


@st.cache_data(ttl=600, show_spinner="米国フロー指標を計算中…")
def _cached_us_report(target_date: str):
    """日付ごとにレポートをキャッシュする。

    Streamlit はウィジェット操作のたびにスクリプト全体を再実行するため、
    キャッシュしないと銘柄を切り替えるだけで毎回フル計算が走る（実測2.3秒）。
    """
    short_df, price_df, si_df = _cached_us_flow_frames()
    return build_daily_report(target_date, short_df, price_df, short_interest_df=si_df)


def _render_us_flow_tab() -> None:
    """米国個別株のショートフロー（FINRA報告分）を表示する。"""
    st.subheader("🇺🇸 米国ショートフロー（FINRA CNMS）")
    st.caption(
        "AI半導体・メモリ・AIインフラ・ハイパースケーラー・SaaS の個別銘柄と、"
        "SMH / SOXX / DRAM / IGV / QQQ / SPY を毎営業日 08:37 JST に自動取得します。"
        "水準は銘柄ごとに大きく違うため、絶対値ではなく銘柄自身の過去分布との比較（Zスコア）で判断します。"
    )

    try:
        short_df, price_df, _ = _cached_us_flow_frames()
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

    # --- 空売り残高（隔週） ---
    _render_us_short_interest(report)

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


def _render_us_short_interest(report: dict) -> None:
    """空売り残高を基準日・経過日数つきで表示する。"""
    view = report.get("short_interest") or {}
    rows = view.get("rows") or []

    st.markdown("#### 空売り残高（隔週・未決済のまま残っている空売り）")
    if not rows:
        st.info(
            "空売り残高の取り込みがまだありません。"
            "日次パイプラインを1回実行すると取り込まれます。"
        )
        return

    elapsed = view.get("days_elapsed")
    cols = st.columns(3)
    cols[0].metric("基準日", view["settlement_date"])
    cols[1].metric("基準日からの経過", "N/A" if elapsed is None else f"{elapsed}日")
    cols[2].metric("対象銘柄", f"{len(rows)}銘柄")

    st.caption(view.get("note", ""))

    frame = pd.DataFrame([{
        "銘柄": r["ticker"],
        "日本語名": r["name_ja"] or "N/A",
        "残高(株)": "N/A" if r["current_short_position"] is None else f"{int(r['current_short_position']):,}",
        "前回(株)": "N/A" if r["previous_short_position"] is None else f"{int(r['previous_short_position']):,}",
        "前回比": "N/A" if r["change_percent"] is None else f"{r['change_percent']:+.2f}%",
        "買い戻し日数": "N/A" if r["days_to_cover"] is None else f"{r['days_to_cover']:.2f}",
    } for r in rows])
    st.dataframe(frame, use_container_width=True, hide_index=True)


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
