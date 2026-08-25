"""
日経平均・TOPIX チャート（空売りチャートの直下に併置）の決定論テスト。ネットワーク非依存。

TOPIX は無料・認証不要の範囲に四本値が存在しない（2026-08-25 調査）。
「TOPIX は必ずライン」「四本値が無ければ日経もラインへ落ちる」を壊さないことを固定する。
"""
import datetime as dt

import pandas as pd

import src.data_fetcher.us_price_client as us_price_client
import src.macro_context.market_quotes as mq
from app.streamlit_app import (
    add_ema_columns,
    build_nikkei_figure,
    build_topix_figure,
    clip_to_window,
)


def _close_frame():
    """_fetch_site_nikkei_topix() が返す形（date/nikkei/topix/nt_ratio）"""
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]),
        "nikkei": [65000.0, 65300.0, 66216.79, 66016.36],
        "topix": [4000.0, 4012.31, 4059.73, 4067.29],
        "nt_ratio": [16.25, 16.27, 16.31, 16.23],
    })


def _ohlc_frame():
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-08-20", "2026-08-21"]),
        "open": [65300.0, 66200.0],
        "high": [66300.0, 66400.0],
        "low": [65200.0, 65900.0],
        "close": [66216.79, 66016.36],
    })


def _trace_types(fig):
    return [type(trace).__name__ for trace in fig.data]


class _StubPriceClient:
    """UsPriceClient の差し替え。要求されたティッカーを記録する。"""

    last_ticker = None

    def __init__(self, records):
        self._records = records

    def get_daily_ohlcv(self, ticker, from_date, to_date):
        _StubPriceClient.last_ticker = ticker
        return self._records


# ──────────────────────────────────────────────────────────────
# 終値時系列（日経平均・TOPIX 共通）
# ──────────────────────────────────────────────────────────────
def test_close_history_filters_by_date_range(monkeypatch):
    monkeypatch.setattr(mq, "_fetch_site_nikkei_topix", _close_frame)

    out = mq.fetch_nikkei_topix_close_history("2026-08-19", "2026-08-20")

    assert list(out.columns) == ["date", "nikkei", "topix"]
    assert len(out) == 2
    assert out["date"].min() == pd.Timestamp("2026-08-19")
    assert out["date"].max() == pd.Timestamp("2026-08-20")


def test_close_history_without_range_returns_everything(monkeypatch):
    monkeypatch.setattr(mq, "_fetch_site_nikkei_topix", _close_frame)

    assert len(mq.fetch_nikkei_topix_close_history()) == 4


def test_close_history_returns_none_when_source_fails(monkeypatch):
    monkeypatch.setattr(mq, "_fetch_site_nikkei_topix", lambda: None)

    assert mq.fetch_nikkei_topix_close_history("2026-08-01", "2026-08-31") is None


def test_site_series_is_stamped_to_the_jst_trading_day(monkeypatch):
    """nikkei225jp.com の足は 15:00 UTC（＝翌 00:00 JST）スタンプ。

    生の ms をそのまま日付にすると取引日が1日前へずれ、Yahoo の四本値や
    空売り比率チャートと横軸が食い違う（2026-08-25 に実データで検出して修正）。
    """
    ms = int(dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(
        mq,
        "_load_series",
        lambda code: [[ms, 66016.36 if code == mq._NT_NIKKEI_CODE else 4067.29]],
    )

    out = mq._fetch_site_nikkei_topix()

    assert out["date"].iloc[0] == pd.Timestamp("2026-08-21")


# ──────────────────────────────────────────────────────────────
# 日経平均の四本値（Yahoo ^N225。既存 UsPriceClient を再利用する）
# ──────────────────────────────────────────────────────────────
def test_ohlc_history_converts_records_and_uses_n225(monkeypatch):
    records = [
        {"Date": "2026-08-20", "Open": 65300.0, "High": 66300.0, "Low": 65200.0, "Close": 66216.79},
        {"Date": "2026-08-21", "Open": 66200.0, "High": 66400.0, "Low": 65900.0, "Close": 66016.36},
    ]
    monkeypatch.setattr(us_price_client, "UsPriceClient", lambda: _StubPriceClient(records))

    out = mq.fetch_nikkei_ohlc_history("2026-08-20", "2026-08-21")

    assert list(out.columns) == ["date", "open", "high", "low", "close"]
    assert len(out) == 2
    assert _StubPriceClient.last_ticker == "^N225"


def test_ohlc_history_drops_incomplete_bars(monkeypatch):
    """休場・未確定の足は四本値が None で来る。欠けた足でロウソクを描かせない。"""
    records = [
        {"Date": "2026-08-20", "Open": 65300.0, "High": 66300.0, "Low": 65200.0, "Close": 66216.79},
        {"Date": "2026-08-21", "Open": None, "High": None, "Low": None, "Close": None},
    ]
    monkeypatch.setattr(us_price_client, "UsPriceClient", lambda: _StubPriceClient(records))

    out = mq.fetch_nikkei_ohlc_history("2026-08-20", "2026-08-21")

    assert len(out) == 1


def test_ohlc_history_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr(us_price_client, "UsPriceClient", lambda: _StubPriceClient([]))

    assert mq.fetch_nikkei_ohlc_history("2026-08-20", "2026-08-21") is None


# ──────────────────────────────────────────────────────────────
# 図の組み立て
# ──────────────────────────────────────────────────────────────
def test_nikkei_figure_uses_candlestick_when_requested():
    fig = build_nikkei_figure(_close_frame(), _ohlc_frame(), "ロウソク足")

    assert _trace_types(fig) == ["Candlestick"]


def test_nikkei_figure_uses_line_by_default():
    fig = build_nikkei_figure(_close_frame(), _ohlc_frame(), "ライン")

    assert _trace_types(fig) == ["Scatter"]


def test_nikkei_figure_falls_back_to_line_without_ohlc():
    """四本値が取れなかった日にロウソク足を選んでも、空チャートにせずラインへ落とす。"""
    fig = build_nikkei_figure(_close_frame(), None, "ロウソク足")

    assert _trace_types(fig) == ["Scatter"]


def test_topix_figure_is_always_a_line():
    """TOPIX は四本値が存在しない。代理ETFなどでロウソク足化しない担保。"""
    fig = build_topix_figure(_close_frame())

    assert _trace_types(fig) == ["Scatter"]


def test_index_axes_align_with_the_short_ratio_chart():
    """横軸を上の空売りチャートに揃える（同時に見比べるのが目的）。"""
    fig = build_topix_figure(_close_frame(), ("2026-08-18", "2026-08-21"))
    start, end = (pd.to_datetime(value) for value in fig.layout.xaxis.range)

    assert start < pd.Timestamp("2026-08-18")
    assert end > pd.Timestamp("2026-08-21")


def test_latest_candle_is_not_clipped_at_the_right_edge():
    """終端ぴったりに範囲を切ると最新の足が半分で切れる（2026-08-25 に実画面で発生）。

    ロウソク1本は前後0.5日ぶんの幅を持つので、余白は半日より広く1日より狭くする。
    """
    fig = build_nikkei_figure(_close_frame(), _ohlc_frame(), "ロウソク足", ("2026-08-20", "2026-08-21"))
    start, end = (pd.to_datetime(value) for value in fig.layout.xaxis.range)

    assert end - pd.Timestamp("2026-08-21") > pd.Timedelta(hours=12)
    assert end - pd.Timestamp("2026-08-21") < pd.Timedelta(days=1)
    assert pd.Timestamp("2026-08-20") - start > pd.Timedelta(hours=12)


# ──────────────────────────────────────────────────────────────
# EMA（日経平均に重ねる指数平滑移動平均）
# ──────────────────────────────────────────────────────────────
def test_add_ema_columns_adds_one_column_per_period():
    out = add_ema_columns(_ohlc_frame(), "close", (20, 60, 120))

    assert {"ema20", "ema60", "ema120"} <= set(out.columns)


def test_ema_uses_the_warmup_data_before_the_display_window():
    """表示範囲だけで計算すると窓の先頭が初期値に引きずられ、別の線になる。"""
    long_series = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=200, freq="D"),
        "close": [60000.0 + i * 10 for i in range(200)],
    })
    window = ("2026-07-01", "2026-07-15")

    with_warmup = clip_to_window(add_ema_columns(long_series, "close", (20,)), *window)
    without_warmup = add_ema_columns(clip_to_window(long_series, *window), "close", (20,))

    assert with_warmup["ema20"].iloc[0] != without_warmup["ema20"].iloc[0]


def test_clip_to_window_keeps_only_the_display_range():
    long_series = pd.DataFrame({
        "date": pd.date_range("2026-08-01", periods=30, freq="D"),
        "close": [60000.0] * 30,
    })

    out = clip_to_window(long_series, "2026-08-10", "2026-08-12")

    assert len(out) == 3
    assert out["date"].min() == pd.Timestamp("2026-08-10")
    assert out["date"].max() == pd.Timestamp("2026-08-12")


def test_nikkei_figure_overlays_selected_emas_on_candles():
    ohlc = add_ema_columns(_ohlc_frame(), "close", (20, 60))

    fig = build_nikkei_figure(_close_frame(), ohlc, "ロウソク足", ema_periods=(20, 60))

    assert _trace_types(fig) == ["Candlestick", "Scatter", "Scatter"]
    assert [t.name for t in fig.data[1:]] == ["EMA20", "EMA60"]
    assert fig.layout.showlegend is True


def test_nikkei_figure_overlays_emas_on_the_line_too():
    close = add_ema_columns(_close_frame(), "nikkei", (20,))

    fig = build_nikkei_figure(close, None, "ライン", ema_periods=(20,))

    assert _trace_types(fig) == ["Scatter", "Scatter"]
    assert fig.data[1].name == "EMA20"


def test_no_ema_selected_keeps_the_chart_clean():
    fig = build_nikkei_figure(_close_frame(), _ohlc_frame(), "ロウソク足", ema_periods=())

    assert _trace_types(fig) == ["Candlestick"]
    assert fig.layout.showlegend is False


def test_topix_never_gets_an_ema_overlay():
    """EMAは日経平均だけの機能。TOPIX側に混ぜない。"""
    close = add_ema_columns(_close_frame(), "topix", (20,))

    fig = build_topix_figure(close)

    assert _trace_types(fig) == ["Scatter"]
