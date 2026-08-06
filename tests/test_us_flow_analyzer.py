"""米国ショートフローのローリング統計テスト（純関数・ネットワーク非依存）。"""
import pandas as pd
import pytest

from src.analyzer.us_flow_analyzer import (
    build_flow_metrics,
    close_location_value,
    percentile_rank,
    zscore,
)


# ------------------------------------------------------------------
# Zスコア
# ------------------------------------------------------------------

def test_zscore_returns_none_when_samples_are_insufficient():
    """窓幅の80%に満たなければ判定しない（少ないサンプルで断定しない）。"""
    history = [40.0] * 15          # window=20 に対し 15件 = 75%

    assert zscore(history, 50.0, window=20) is None


def test_zscore_computes_when_coverage_is_met():
    history = [40.0] * 16 + [50.0] * 4   # 20件・平均42.0
    result = zscore(history, 50.0, window=20)

    assert result is not None
    assert result == pytest.approx(2.0, abs=0.01)


def test_zscore_returns_none_when_standard_deviation_is_zero():
    """全期間同値なら分母0。例外を投げずに None を返す。"""
    assert zscore([45.0] * 20, 50.0, window=20) is None


def test_zscore_returns_none_for_missing_current_value():
    assert zscore([40.0] * 20, None, window=20) is None


def test_zscore_ignores_missing_history_values():
    """欠損は詰めずに除外する（前日値で埋めない）。"""
    history = [40.0, None, 42.0] + [41.0] * 18

    assert zscore(history, 41.0, window=20) is not None


def test_zscore_uses_only_the_most_recent_window():
    """窓幅を超える古い履歴は判定に混ぜない。"""
    old_noise = [1000.0] * 50
    recent = [40.0] * 19 + [44.0]

    result = zscore(old_noise + recent, 44.0, window=20)

    assert result is not None
    assert result == pytest.approx(4.359, abs=0.01)


# ------------------------------------------------------------------
# パーセンタイル
# ------------------------------------------------------------------

def test_percentile_rank_positions_value_within_distribution():
    history = [float(i) for i in range(60)]   # 0〜59

    assert percentile_rank(history, 29.0, window=60) == pytest.approx(50.0)
    assert percentile_rank(history, 100.0, window=60) == pytest.approx(100.0)


def test_percentile_rank_returns_none_when_samples_are_insufficient():
    assert percentile_rank([1.0] * 10, 5.0, window=60) is None


# ------------------------------------------------------------------
# 終値位置(CLV)
# ------------------------------------------------------------------

def test_clv_boundaries():
    assert close_location_value(high=110, low=100, close=110) == pytest.approx(1.0)
    assert close_location_value(high=110, low=100, close=100) == pytest.approx(-1.0)
    assert close_location_value(high=110, low=100, close=105) == pytest.approx(0.0)


def test_clv_returns_none_when_range_is_zero_or_values_missing():
    assert close_location_value(high=100, low=100, close=100) is None
    assert close_location_value(high=None, low=100, close=100) is None


# ------------------------------------------------------------------
# DataFrame 組み立て
# ------------------------------------------------------------------

def _short_df(ratios: list[float], ticker: str = "NVDA") -> pd.DataFrame:
    return pd.DataFrame([
        {
            "date": f"2026-01-{i + 1:02d}",
            "ticker": ticker,
            "short_ratio_pct": ratio,
        }
        for i, ratio in enumerate(ratios)
    ])


def test_build_flow_metrics_leaves_early_rows_unjudged():
    """履歴が足りない序盤は欠損のまま。行を捏造も補間もしない。"""
    df = build_flow_metrics(_short_df([40.0 + i * 0.1 for i in range(25)]))

    assert len(df) == 25
    assert pd.isna(df.loc[0, "z20"])
    # 窓幅20に対し最低16件（80%）必要。15件では判定しない
    assert pd.isna(df.loc[15, "z20"])
    assert pd.notna(df.loc[16, "z20"])
    assert df["z60"].notna().sum() == 0       # 60日窓はまだ判定不能


def test_build_flow_metrics_computes_per_ticker_independently():
    """銘柄をまたいで履歴が混ざらない。

    水準が違っても分布の形が同じなら同じZスコアになる。これが
    「絶対閾値ではなく銘柄自身の過去分布で測る」という設計そのもの。
    """
    shape = [float(i % 2) for i in range(19)]
    a = _short_df([30.0 + v for v in shape] + [35.0], ticker="NVDA")
    b = _short_df([70.0 + v for v in shape] + [75.0], ticker="SMH")

    df = build_flow_metrics(pd.concat([a, b], ignore_index=True))
    latest = df[df["date"] == "2026-01-20"].set_index("ticker")

    assert pd.notna(latest.loc["NVDA", "z20"])
    assert latest.loc["NVDA", "z20"] == pytest.approx(latest.loc["SMH", "z20"])


def test_build_flow_metrics_adds_price_context_when_available():
    short_df = _short_df([40.0] * 3)
    price_df = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "NVDA", "high": 105, "low": 95, "close": 100, "market_volume": 1000},
        {"date": "2026-01-02", "ticker": "NVDA", "high": 112, "low": 100, "close": 110, "market_volume": 1200},
        {"date": "2026-01-03", "ticker": "NVDA", "high": 115, "low": 104, "close": 105, "market_volume": 900},
    ])

    df = build_flow_metrics(short_df, price_df)

    assert df.loc[1, "daily_return"] == pytest.approx(0.10)
    assert df.loc[1, "clv"] == pytest.approx(0.6667, abs=1e-3)
    assert pd.isna(df.loc[0, "daily_return"])      # 前日終値がない


def test_build_flow_metrics_handles_empty_input():
    assert build_flow_metrics(pd.DataFrame()).empty
    assert build_flow_metrics(None).empty
