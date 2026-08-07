"""バスケット集計とETF乖離のテスト。"""
import pandas as pd
import pytest

from src.analyzer.us_basket import (
    build_basket_ratio_series,
    compute_divergence,
    interpret_divergence,
)


def _row(date, ticker, short_volume, total_volume):
    ratio = None
    if total_volume:
        ratio = short_volume / total_volume * 100
    return {
        "date": date,
        "ticker": ticker,
        "short_volume": short_volume,
        "reported_total_volume": total_volume,
        "short_ratio_pct": ratio,
    }


def test_basket_uses_volume_weighting_not_simple_average():
    """小型株の極端値に引きずられないこと。

    大型株: 比率40%（出来高1,000,000）
    小型株: 比率90%（出来高10,000）
    単純平均なら65%になるが、ボリューム加重では約40.5%にとどまる。
    """
    df = pd.DataFrame([
        _row("2026-01-01", "NVDA", 400_000, 1_000_000),
        _row("2026-01-01", "ONTO", 9_000, 10_000),
    ])

    series = build_basket_ratio_series(df, ["NVDA", "ONTO"])

    assert len(series) == 1
    ratio = series.loc[0, "ratio"]
    assert ratio == pytest.approx(40.495, abs=0.01)
    assert ratio != pytest.approx(65.0, abs=1.0)   # 単純平均ではない


def test_basket_skips_days_with_zero_or_missing_denominator():
    """分母が立たない日は行を作らない（補間しない）。"""
    df = pd.DataFrame([
        _row("2026-01-01", "NVDA", 400_000, 1_000_000),
        _row("2026-01-02", "NVDA", 100, 0),
        _row("2026-01-03", "NVDA", None, None),
    ])

    series = build_basket_ratio_series(df, ["NVDA"])

    assert series["date"].tolist() == ["2026-01-01"]


def test_basket_counts_members_actually_present():
    """欠損銘柄がある日を検知できるよう、実在した構成銘柄数を残す。"""
    df = pd.DataFrame([
        _row("2026-01-01", "NVDA", 400_000, 1_000_000),
        _row("2026-01-01", "AMD", 200_000, 500_000),
        _row("2026-01-02", "NVDA", 400_000, 1_000_000),   # AMDが欠測
    ])

    series = build_basket_ratio_series(df, ["NVDA", "AMD"]).set_index("date")

    assert series.loc["2026-01-01", "members_present"] == 2
    assert series.loc["2026-01-02", "members_present"] == 1


def test_basket_returns_empty_for_unknown_members():
    df = pd.DataFrame([_row("2026-01-01", "NVDA", 400_000, 1_000_000)])

    assert build_basket_ratio_series(df, ["NOPE"]).empty
    assert build_basket_ratio_series(pd.DataFrame(), ["NVDA"]).empty


# ------------------------------------------------------------------
# ETF乖離
# ------------------------------------------------------------------

def _series_for_divergence() -> pd.DataFrame:
    """20営業日ぶんの履歴＋最終日を作る。

    構成銘柄（NVDA/AMD）は最終日にショート比率が跳ね上がり、
    ETF（SMH）は平常のまま。＝個別選別型の乖離になるはず。
    """
    rows = []
    for i in range(20):
        date = f"2026-01-{i + 1:02d}"
        wobble = (i % 2) * 10_000     # 標準偏差を0にしないための揺らぎ
        rows.append(_row(date, "NVDA", 400_000 + wobble, 1_000_000))
        rows.append(_row(date, "AMD", 200_000 + wobble, 500_000))
        rows.append(_row(date, "SMH", 100_000 + wobble, 250_000))

    # 最終日: 個別だけショート急増、ETFは平常
    rows.append(_row("2026-01-21", "NVDA", 700_000, 1_000_000))
    rows.append(_row("2026-01-21", "AMD", 350_000, 500_000))
    rows.append(_row("2026-01-21", "SMH", 105_000, 250_000))
    return pd.DataFrame(rows)


def test_divergence_flags_stock_selection_when_only_constituents_spike(monkeypatch):
    from src.analyzer import us_basket

    monkeypatch.setattr(us_basket, "basket_members", lambda name: ["NVDA", "AMD"])

    result = compute_divergence(
        _series_for_divergence(), "SEMI20", "SMH", target_date="2026-01-21"
    )

    assert result["basket_z20"] is not None
    assert result["etf_z20"] is not None
    assert result["divergence"] < -1.5
    assert "個別側" in result["interpretation"]


def test_divergence_is_none_when_history_is_insufficient(monkeypatch):
    """履歴が足りずZスコアが出せない場合は乖離を出さない。"""
    from src.analyzer import us_basket

    monkeypatch.setattr(us_basket, "basket_members", lambda name: ["NVDA"])
    df = pd.DataFrame([_row("2026-01-01", "NVDA", 400_000, 1_000_000)])

    result = compute_divergence(df, "SEMI20", "SMH", target_date="2026-01-01")

    assert result["divergence"] is None
    assert result["interpretation"] == "判定不能（データ不足）"


def test_interpretation_thresholds():
    assert "ETF側" in interpret_divergence(2.0)
    assert "個別側" in interpret_divergence(-2.0)
    assert "連動" in interpret_divergence(0.5)
    assert interpret_divergence(None) == "判定不能（データ不足）"
