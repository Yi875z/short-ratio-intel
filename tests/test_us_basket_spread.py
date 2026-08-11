"""ロング候補 / ショート候補のバスケット対比較のテスト。"""
import pandas as pd
import pytest

from src.analyzer import us_basket
from src.analyzer.us_basket import compute_basket_spread, interpret_spread


def _row(date, ticker, short_volume, total_volume):
    return {
        "date": date,
        "ticker": ticker,
        "short_volume": short_volume,
        "reported_total_volume": total_volume,
        "short_ratio_pct": short_volume / total_volume * 100,
    }


def _frames(short_side_spike: float) -> pd.DataFrame:
    """20日の履歴＋最終日。ショート側だけ最終日に跳ねる。

    水準はロング側40%台・ショート側60%台とわざと違えてある。
    比率の引き算ではなくZスコアの差で見ていることを確かめるため。
    """
    rows = []
    for i in range(20):
        date = f"2026-01-{i + 1:02d}"
        wobble = (i % 2) * 10_000
        rows.append(_row(date, "LONGA", 400_000 + wobble, 1_000_000))
        rows.append(_row(date, "SHORTA", 600_000 + wobble, 1_000_000))
    rows.append(_row("2026-01-21", "LONGA", 405_000, 1_000_000))
    rows.append(_row("2026-01-21", "SHORTA", short_side_spike, 1_000_000))
    return pd.DataFrame(rows)


@pytest.fixture
def patched_baskets(monkeypatch):
    monkeypatch.setattr(
        us_basket, "basket_members",
        lambda name: ["LONGA"] if name == "LONG_B" else ["SHORTA"],
    )


def test_spread_flags_short_side_crowding(patched_baskets):
    """ショート側だけ普段より売られていれば、差がプラスに開く。"""
    result = compute_basket_spread(
        _frames(short_side_spike=900_000), "LONG_B", "SHORT_B", "2026-01-21"
    )

    assert result["spread"] > 1.5
    assert "SHORT_B" in result["interpretation"]


def test_spread_uses_zscore_not_raw_ratio_difference(patched_baskets):
    """水準差（60%台 vs 40%台）ではなく、過去分布からの乖離度を比べている。"""
    result = compute_basket_spread(
        _frames(short_side_spike=605_000), "LONG_B", "SHORT_B", "2026-01-21"
    )

    # 比率の生の差は約20ポイントあるが、どちらも平常圏なので差は小さい
    assert result["long_ratio"] == pytest.approx(40.5, abs=0.1)
    assert result["short_ratio"] == pytest.approx(60.5, abs=0.1)
    assert abs(result["spread"]) < 1.5
    assert "偏っていない" in result["interpretation"]


def test_spread_is_none_when_history_is_insufficient(patched_baskets):
    df = pd.DataFrame([
        _row("2026-01-01", "LONGA", 400_000, 1_000_000),
        _row("2026-01-01", "SHORTA", 600_000, 1_000_000),
    ])

    result = compute_basket_spread(df, "LONG_B", "SHORT_B", "2026-01-01")

    assert result["spread"] is None
    assert result["interpretation"] == "判定不能（データ不足）"


def test_interpretation_wording():
    assert "SHORT_B側" in interpret_spread(2.0, "LONG_B", "SHORT_B")
    assert "LONG_B側" in interpret_spread(-2.0, "LONG_B", "SHORT_B")
    assert "偏っていない" in interpret_spread(0.4, "LONG_B", "SHORT_B")
    assert interpret_spread(None, "LONG_B", "SHORT_B") == "判定不能（データ不足）"


def test_configured_pairs_reference_defined_baskets():
    """設定したペアが実在するバスケットを指しているか（打ち間違い検出）。"""
    from config.us_universe import BASKET_PAIRS, BASKETS

    for pair in BASKET_PAIRS:
        assert pair["long"] in BASKETS, pair["long"]
        assert pair["short"] in BASKETS, pair["short"]


def test_configured_divergence_pairs_reference_defined_baskets():
    from config.us_universe import BASKETS, DIVERGENCE_PAIRS, US_UNIVERSE

    for etf, basket in DIVERGENCE_PAIRS:
        assert etf in US_UNIVERSE, etf
        assert basket in BASKETS, basket


def test_every_monitored_ticker_has_japanese_name_and_category():
    """銘柄を足したときに日本語名・AI種別の登録漏れを検出する。"""
    from config.us_universe import US_UNIVERSE, ai_category, japanese_name

    missing_name = [t for t in US_UNIVERSE if not japanese_name(t)]
    missing_category = [t for t in US_UNIVERSE if not ai_category(t)]

    assert not missing_name, f"日本語名が未登録: {missing_name}"
    assert not missing_category, f"AI種別が未登録: {missing_category}"


def test_every_basket_member_is_monitored():
    """バスケットの構成銘柄が監視対象から漏れていないこと。"""
    from config.us_universe import BASKETS, US_UNIVERSE, basket_members

    for name in BASKETS:
        missing = [t for t in basket_members(name) if t not in US_UNIVERSE]
        assert not missing, f"{name} の構成銘柄が監視対象外: {missing}"
