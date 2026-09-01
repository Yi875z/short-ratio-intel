"""
売り圧力レジーム判定のテスト（ネットワーク・DB非依存）。

依頼の判定要件を回帰として固定する:
  - 6レジームそれぞれが代表ケースで成立する
  - 単一の固定閾値だけで判定しない（同じ比率でも分布次第で結論が変わる）
  - 比率だけ高く実額が増えていない日を SELL_PRESSURE と読まない
  - 価格規制なしが多いことだけを弱気の根拠にしない
  - 入力が欠けているレジームは候補から外す（欠損を0とみなさない）
"""
import pytest

from src.analyzer.pressure_metrics import (
    BreadthBlock,
    ChangeBlock,
    PressureMetrics,
    PriceBlock,
    RatioBlock,
    ValueBlock,
)
from src.analyzer.pressure_regime import (
    REGIME_ABSORPTION,
    REGIME_BROAD_DE_RISKING,
    REGIME_NEUTRAL,
    REGIME_SELL_PRESSURE,
    REGIME_SHORT_COVER,
    REGIME_THIN_MARKET,
    PressureRegimeClassifier,
)


def _metrics(
    *,
    short_z=0.0, short_vs_avg=0.0,
    volume_z=0.0, volume_vs_avg=0.0,
    ratio_z=0.0, ratio_latest=40.0, ratio_dod_pct=0.0,
    with_ratio_z=0.0,
    topix=0.5,
    net_breadth=0.0,
    without_share=19.0,
    date="2026-08-28",
):
    """レジーム判定に必要な指標だけを組み立てたテスト用ファクトリ。"""
    missing = []
    if topix is None:
        missing.append("TOPIX騰落率")
    if net_breadth is None:
        missing.append("騰落銘柄数")

    return PressureMetrics(
        date=date,
        ratios=RatioBlock(
            total_short_pct=ratio_latest,
            with_restriction_pct=ratio_latest * 0.8,
            without_restriction_pct=ratio_latest * 0.2,
            without_share_pct=without_share,
        ),
        values=ValueBlock(
            total_short_va=4_000_000.0, market_volume_va=10_000_000.0,
        ),
        short_value_change=ChangeBlock(
            label="総空売り代金", latest=4_000_000.0,
            vs_avg_pct=short_vs_avg, zscore=short_z, sample_size=20,
        ),
        market_volume_change=ChangeBlock(
            label="市場売買代金", latest=10_000_000.0,
            vs_avg_pct=volume_vs_avg, zscore=volume_z, sample_size=20,
        ),
        total_ratio_change=ChangeBlock(
            label="空売り比率", latest=ratio_latest,
            dod_pct=ratio_dod_pct, zscore=ratio_z, sample_size=20,
        ),
        with_ratio_change=ChangeBlock(
            label="価格規制あり比率", latest=ratio_latest * 0.8,
            zscore=with_ratio_z, sample_size=20,
        ),
        price=PriceBlock(topix_change_pct=topix, available=topix is not None),
        breadth=BreadthBlock(
            scope="TSE_PRIME", scope_label="プライム",
            advancing=800, declining=700,
            net_breadth=net_breadth, available=net_breadth is not None,
        ),
        missing_inputs=tuple(missing),
    )


def _classify(**kwargs):
    return PressureRegimeClassifier().classify(_metrics(**kwargs))


# ------------------------------------------------------------------
# 各レジームの代表ケース
# ------------------------------------------------------------------
def test_売り圧力は実額増と規制あり主導と価格下落が揃って成立する():
    result = _classify(
        short_z=1.5, short_vs_avg=12.0, with_ratio_z=1.0, topix=-1.2,
        ratio_z=1.2, volume_z=0.5,
    )
    assert result.primary == REGIME_SELL_PRESSURE
    assert result.confidence == "high"
    assert any("総空売り代金" in reason for reason in result.reasons)
    assert any("TOPIX" in reason for reason in result.reasons)


def test_薄商いは比率が高くても実額が増えていなければ成立する():
    """依頼の核心。分母の縮小による見かけの高比率を隔離する。"""
    result = _classify(
        volume_z=-1.5, volume_vs_avg=-22.0,   # 商いが細い
        ratio_z=1.4,                          # 比率は高い
        short_z=0.1, short_vs_avg=-5.0,       # 実額は増えていない
        topix=-0.3,
    )
    assert result.primary == REGIME_THIN_MARKET
    assert REGIME_SELL_PRESSURE not in (result.primary, *result.also_matched)


def test_Zは低くても5日平均比が伸びていれば薄商いと呼ばない():
    """2026-06-04 の実データで見つかった矛盾の回帰。

    20日分布では平常（Z +0.46）でも、直近5日が静かだと平均比は +40% に跳ねる。
    片方だけで判定すると「実額は増えていない」という理由文が画面の数字と食い違う。
    両方が同意したときだけ薄商いと呼ぶ。
    """
    result = _classify(
        volume_z=-0.51, volume_vs_avg=-19.7,
        ratio_z=0.59,
        short_z=0.46, short_vs_avg=40.2,   # Zは低いが直近比では大きく増えている
        topix=-1.11, net_breadth=-0.43,
    )
    assert result.primary != REGIME_THIN_MARKET

    verdict = next(v for v in result.verdicts if v.regime == REGIME_THIN_MARKET)
    assert verdict.matched is False
    assert any("5日平均比" in item for item in verdict.unsatisfied)


def test_吸収は売りが出ているのに価格が下がらない日に成立する():
    result = _classify(short_z=1.2, short_vs_avg=10.0, topix=0.8, with_ratio_z=0.2)
    assert result.primary == REGIME_ABSORPTION


def test_全面リスク回避は空売り比率の高低を問わず成立する():
    result = _classify(
        topix=-1.8, net_breadth=-0.55, volume_z=0.9,
        ratio_z=-0.5, short_z=-0.2,   # 空売りはむしろ低調
    )
    assert result.primary == REGIME_BROAD_DE_RISKING


def test_買い戻し候補は比率低下と実額減と価格上昇で成立する():
    # 比率 43.0%、前日比 -10% → 約 -4.8pt の低下
    result = _classify(
        ratio_latest=43.0, ratio_dod_pct=-10.0,
        short_vs_avg=-12.0, topix=1.4, short_z=-0.8,
    )
    assert result.primary == REGIME_SHORT_COVER


def test_どれも成立しなければ中立になる():
    result = _classify(short_z=0.1, volume_z=0.1, ratio_z=0.1, topix=0.05,
                       net_breadth=0.02, short_vs_avg=1.0)
    assert result.primary == REGIME_NEUTRAL
    assert result.reasons


# ------------------------------------------------------------------
# 単一の固定閾値で判定しないこと
# ------------------------------------------------------------------
def test_同じ空売り比率でも分布次第で結論が変わる():
    """絶対水準44%は同じでも、その水準が平常か異常かで判定が分かれる。"""
    common = dict(ratio_latest=44.0, topix=-1.0, short_vs_avg=10.0)

    # 44% がこの銘柄群にとって異常に高く、実額も伴う日
    unusual = _classify(**common, ratio_z=1.8, short_z=1.5, with_ratio_z=1.2)
    # 44% が平常運転で、実額も伸びていない日
    usual = _classify(**common, ratio_z=0.0, short_z=0.0, with_ratio_z=0.0)

    assert unusual.primary == REGIME_SELL_PRESSURE
    assert usual.primary != REGIME_SELL_PRESSURE


def test_価格が下げていても実額が増えていなければ売り圧力にしない():
    result = _classify(topix=-1.5, ratio_z=1.5, with_ratio_z=1.5,
                       short_z=-0.5, short_vs_avg=-8.0)
    assert result.primary != REGIME_SELL_PRESSURE


# ------------------------------------------------------------------
# 価格規制なしを弱気と断定しない
# ------------------------------------------------------------------
def test_規制なし構成比が高いと方向性売りの確信度を下げる():
    base = dict(short_z=1.5, short_vs_avg=12.0, with_ratio_z=1.0, topix=-1.2)

    normal = _classify(**base, without_share=19.0)
    diluted = _classify(**base, without_share=42.0)

    assert normal.primary == diluted.primary == REGIME_SELL_PRESSURE
    assert normal.confidence == "high"
    assert diluted.confidence == "medium"       # 1段下がる
    assert any("裁定・ヘッジ" in caveat for caveat in diluted.caveats)


def test_規制なしが多いだけでは売り圧力と判定しない():
    """規制なしは裁定・ヘッジ由来のことがあるため、それ単体を弱気の根拠にしない。"""
    result = _classify(without_share=55.0, short_z=0.0, with_ratio_z=0.0,
                       topix=0.3, short_vs_avg=0.0)
    assert result.primary != REGIME_SELL_PRESSURE


# ------------------------------------------------------------------
# 欠損の扱い（0とみなさない）
# ------------------------------------------------------------------
def test_騰落銘柄数が無い日は全面リスク回避を判定しない():
    """欠損を0と読むと「広がりは中立」と誤解釈してしまう。候補から外す。"""
    result = _classify(topix=-1.8, net_breadth=None, volume_z=0.9)

    assert result.primary != REGIME_BROAD_DE_RISKING
    verdict = next(v for v in result.verdicts if v.regime == REGIME_BROAD_DE_RISKING)
    assert verdict.matched is False
    assert "騰落銘柄数" in verdict.missing_inputs
    assert verdict.evaluable is False


def test_TOPIXが無い日は価格反応を使うレジームを判定しない():
    result = _classify(topix=None, short_z=1.5, short_vs_avg=12.0, with_ratio_z=1.0)

    for regime in (REGIME_SELL_PRESSURE, REGIME_ABSORPTION, REGIME_SHORT_COVER):
        verdict = next(v for v in result.verdicts if v.regime == regime)
        assert verdict.matched is False
        assert "TOPIX騰落率" in verdict.missing_inputs


def test_入力欠損で中立になった場合は確信度を下げて理由に残す():
    result = _classify(topix=None, net_breadth=None)

    assert result.primary == REGIME_NEUTRAL
    assert result.confidence == "low"
    assert any("不足" in reason for reason in result.reasons)
    assert "TOPIX騰落率" in result.missing_inputs
    assert "騰落銘柄数" in result.missing_inputs


def test_空売り側の指標が欠けても他のレジームは評価できる():
    """一部の入力欠損で判定全体が落ちない。"""
    metrics = _metrics(topix=-1.8, net_breadth=-0.55, volume_z=0.9)
    metrics = PressureMetrics(
        date=metrics.date, ratios=metrics.ratios, values=metrics.values,
        short_value_change=ChangeBlock(label="総空売り代金"),  # 全部 None
        market_volume_change=metrics.market_volume_change,
        total_ratio_change=metrics.total_ratio_change,
        with_ratio_change=metrics.with_ratio_change,
        price=metrics.price, breadth=metrics.breadth,
    )
    result = PressureRegimeClassifier().classify(metrics)

    # 空売り代金を使わない全面リスク回避は依然として判定できる
    assert result.primary == REGIME_BROAD_DE_RISKING


# ------------------------------------------------------------------
# 出力の形
# ------------------------------------------------------------------
def test_理由に実測値が入る():
    """閾値を超えたかどうかだけでなく、どれだけ超えたかを残す。"""
    result = _classify(short_z=1.5, short_vs_avg=12.0, with_ratio_z=1.0, topix=-1.2)
    assert any("実測" in reason for reason in result.reasons)


def test_複数成立した場合は第1位以外も残す():
    """買い戻しと吸収は同時に成立しうる。

    空売り代金は直近20日分布では高め（Zスコア +0.8）だが5日平均は下回っており、
    価格は上昇、比率は前日から大きく低下している日。
    """
    result = _classify(
        short_z=0.8, short_vs_avg=-5.0, topix=1.2,
        ratio_latest=43.0, ratio_dod_pct=-10.0,   # 約 -4.8pt の低下
    )
    matched = {result.primary, *result.also_matched}

    assert REGIME_SHORT_COVER in matched
    assert REGIME_ABSORPTION in matched
    # 条件を多く満たした側が第1位になる
    assert result.primary == REGIME_SHORT_COVER


def test_辞書化できる():
    result = _classify(short_z=1.5, short_vs_avg=12.0, with_ratio_z=1.0, topix=-1.2)
    data = result.to_dict()
    assert data["primary"] == REGIME_SELL_PRESSURE
    assert isinstance(data["verdicts"], list)
    assert data["verdicts"][0]["regime"]
