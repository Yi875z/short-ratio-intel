"""4象限パターン分類の境界条件テスト。"""
import pandas as pd

from src.analyzer.us_flow_classifier import (
    INSUFFICIENT_DATA,
    LONG_LIQUIDATION,
    NEUTRAL,
    SELL_PRESSURE,
    SHORT_ABSORBED,
    SQUEEZE_BUILDING,
    classify_flow_metrics,
    classify_row,
    summarize_patterns,
)


def _row(**overrides) -> dict:
    base = {"z20": 0.0, "daily_return": 0.0, "clv": 0.0, "volume_ratio": 1.0}
    base.update(overrides)
    return base


# ------------------------------------------------------------------
# 各象限
# ------------------------------------------------------------------

def test_sell_pressure_requires_all_four_conditions():
    """ショート高進・下落・安値引け・出来高増がそろって初めて売り圧力候補。"""
    hit = _row(z20=2.0, daily_return=-0.03, clv=-0.7, volume_ratio=1.5)
    assert classify_row(hit) == SELL_PRESSURE

    # 出来高が伴わなければ売り圧力とは呼ばない
    assert classify_row(_row(z20=2.0, daily_return=-0.03, clv=-0.7, volume_ratio=1.0)) != SELL_PRESSURE
    # 高値引けなら該当しない
    assert classify_row(_row(z20=2.0, daily_return=-0.03, clv=0.5, volume_ratio=1.5)) != SELL_PRESSURE


def test_short_absorbed_when_price_rises_into_the_close():
    """ショートが多いのに上昇・高値引けなら、売りを買いが吸収した候補。"""
    assert classify_row(_row(z20=2.0, daily_return=0.02, clv=0.6)) == SHORT_ABSORBED


def test_long_liquidation_when_short_is_low_but_price_falls():
    """ショートが少ないのに下げているならロング側の売り主導を疑う。"""
    assert classify_row(_row(z20=-1.5, daily_return=-0.02)) == LONG_LIQUIDATION


def test_squeeze_building_needs_three_consecutive_flat_days():
    """高いショート比率が続いても株価が動かない状態が3営業日続いて初めて蓄積候補。"""
    row = _row(z20=1.2, daily_return=0.001)

    assert classify_row(row, flat_streak=2) == NEUTRAL
    assert classify_row(row, flat_streak=3) == SQUEEZE_BUILDING


def test_neutral_when_nothing_stands_out():
    assert classify_row(_row(z20=0.3, daily_return=0.001)) == NEUTRAL


# ------------------------------------------------------------------
# 判定不能の扱い
# ------------------------------------------------------------------

def test_missing_zscore_is_reported_as_insufficient_not_neutral():
    """Zスコアが出せない日は「シグナルなし」ではなく「判定不能」と区別する。"""
    assert classify_row(_row(z20=None)) == INSUFFICIENT_DATA
    assert classify_row(_row(z20=float("nan"))) == INSUFFICIENT_DATA


def test_missing_price_context_does_not_crash():
    """価格データが無くてもZスコアだけで判定を試み、例外にしない。"""
    assert classify_row({"z20": 2.0}) == NEUTRAL


# ------------------------------------------------------------------
# DataFrame 走査（連続性の判定）
# ------------------------------------------------------------------

def test_flat_streak_is_counted_per_ticker_over_time():
    frame = pd.DataFrame([
        # NVDA: 高ショートで動かない日が3日続く → 3日目に蓄積候補
        {"date": "2026-01-01", "ticker": "NVDA", "z20": 1.2, "daily_return": 0.001, "clv": 0.0, "volume_ratio": 1.0},
        {"date": "2026-01-02", "ticker": "NVDA", "z20": 1.3, "daily_return": -0.001, "clv": 0.0, "volume_ratio": 1.0},
        {"date": "2026-01-03", "ticker": "NVDA", "z20": 1.4, "daily_return": 0.002, "clv": 0.0, "volume_ratio": 1.0},
        # SMH: 同じ日付でも別銘柄。連続カウントは混ざらない
        {"date": "2026-01-03", "ticker": "SMH", "z20": 1.4, "daily_return": 0.002, "clv": 0.0, "volume_ratio": 1.0},
    ])

    result = classify_flow_metrics(frame).set_index(["ticker", "date"])

    assert result.loc[("NVDA", "2026-01-01"), "pattern"] == NEUTRAL
    assert result.loc[("NVDA", "2026-01-02"), "pattern"] == NEUTRAL
    assert result.loc[("NVDA", "2026-01-03"), "pattern"] == SQUEEZE_BUILDING
    assert result.loc[("SMH", "2026-01-03"), "pattern"] == NEUTRAL


def test_streak_resets_when_price_moves():
    frame = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "NVDA", "z20": 1.2, "daily_return": 0.001},
        {"date": "2026-01-02", "ticker": "NVDA", "z20": 1.2, "daily_return": 0.001},
        {"date": "2026-01-03", "ticker": "NVDA", "z20": 1.2, "daily_return": 0.04},   # 大きく動いた
        {"date": "2026-01-04", "ticker": "NVDA", "z20": 1.2, "daily_return": 0.001},
    ])

    patterns = classify_flow_metrics(frame)["pattern"].tolist()

    assert SQUEEZE_BUILDING not in patterns


def test_summarize_counts_every_tag_including_unjudged():
    frame = pd.DataFrame([
        {"date": "2026-01-03", "ticker": "A", "z20": 2.0, "daily_return": 0.02, "clv": 0.6},
        {"date": "2026-01-03", "ticker": "B", "z20": None, "daily_return": 0.02, "clv": 0.6},
    ])

    summary = summarize_patterns(classify_flow_metrics(frame))

    assert summary[SHORT_ABSORBED] == 1
    assert summary[INSUFFICIENT_DATA] == 1


def test_empty_input_returns_empty_frame():
    assert classify_flow_metrics(pd.DataFrame()).empty
    assert summarize_patterns(pd.DataFrame()) == {}
