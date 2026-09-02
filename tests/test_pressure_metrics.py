"""
需給指標層のテスト（ネットワーク・DB非依存）。

依頼の算出ロジック要件をそのまま回帰として固定する:
  - 空売り比率は日次フローとして扱う（残高ではない）
  - 比率と絶対額を必ず分離する
  - 異なる分母・対象市場を混ぜない
  - 価格規制なしを弱気と断定しない（構成比は独立の値として持つ）
  - 欠損を補間しない
"""
import pandas as pd
import pytest

from config.pressure_thresholds import PressureWindows
from src.analyzer.pressure_metrics import (
    build_pressure_metrics,
    format_trillion_yen,
    to_trillion_yen,
)

# 2026-08-28 の実データ（JPX公式PDF・単位は百万円）
REAL_DAY = {
    "date": "2026-08-28",
    "sell_ex_short_va": 5_140_754.0,
    "shrt_with_res_va": 3_201_367.0,
    "shrt_no_res_va": 751_240.0,
    "total_short_va": 3_952_607.0,
    "total_volume_va": 9_093_361.0,
    "short_ratio_pct": 43.47,
}


def _history(rows):
    return pd.DataFrame(rows)


def _day(date, total_volume, total_short, with_res=None, no_res=None, actual=None):
    with_res = total_short * 0.8 if with_res is None else with_res
    no_res = total_short - with_res if no_res is None else no_res
    return {
        "date": date,
        "sell_ex_short_va": total_volume - total_short if actual is None else actual,
        "shrt_with_res_va": with_res,
        "shrt_no_res_va": no_res,
        "total_short_va": total_short,
        "total_volume_va": total_volume,
        # 実データでは取得元から必ず入る列。内訳が欠けた日でもこれだけは残る。
        "short_ratio_pct": round(total_short / total_volume * 100, 2) if total_volume else 0,
    }


# ------------------------------------------------------------------
# 比率（JPXの公式定義と一致すること）
# ------------------------------------------------------------------
def test_比率はJPX公式の分母で算出する():
    """PDF本文の定義 (a)/(d)・(b)/(d)・(c)/(d) と一致することを固定する。

    2026-08-28 の公表値は 実注文56.5% / 規制あり35.2% / 規制なし8.3%。
    """
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]))
    ratios = metrics.ratios

    assert ratios.actual_order_pct == pytest.approx(56.5, abs=0.05)
    assert ratios.with_restriction_pct == pytest.approx(35.2, abs=0.05)
    assert ratios.without_restriction_pct == pytest.approx(8.3, abs=0.05)
    # 空売り比率 = (b+c)/d。DBに入っている公表値 43.47 と一致する
    assert ratios.total_short_pct == pytest.approx(43.47, abs=0.05)


def test_規制なし構成比は総空売りを分母にする():
    """市場全体に対する比率とは別物。弱気/ヘッジの切り分け材料として独立に持つ。"""
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]))
    # 751,240 / 3,952,607
    assert metrics.ratios.without_share_pct == pytest.approx(19.01, abs=0.05)
    # 市場全体に対する比率（8.3%）とは別の値であること
    assert metrics.ratios.without_share_pct != metrics.ratios.without_restriction_pct


def test_売買代金がゼロなら内訳由来の比率は算出しない():
    """ゼロ除算で garbage を作らない。

    ただし空売り比率そのものは取得元から得られているため、
    保存済みの short_ratio_pct をそのまま採用する。
    「内訳が計算できない」と「空売りが無かった」は別の事実。
    """
    row = dict(REAL_DAY, total_volume_va=0.0)
    metrics = build_pressure_metrics("2026-08-28", _history([row]))

    assert metrics.ratios.total_short_pct == pytest.approx(43.47)   # 取得元の値
    assert metrics.ratios.with_restriction_pct is None              # 内訳由来は出さない
    assert metrics.ratios.without_restriction_pct is None
    assert "売買代金" in metrics.missing_inputs


# ------------------------------------------------------------------
# 比率と絶対額の分離
# ------------------------------------------------------------------
def test_比率が同じでも商いが半分なら絶対額は半分になる():
    """依頼の核。比率だけ見ると同じに見える2日を、実額で区別できること。"""
    busy = build_pressure_metrics(
        "2026-08-28", _history([_day("2026-08-28", 10_000_000, 4_000_000)])
    )
    thin = build_pressure_metrics(
        "2026-08-28", _history([_day("2026-08-28", 5_000_000, 2_000_000)])
    )

    assert busy.ratios.total_short_pct == thin.ratios.total_short_pct == 40.0
    assert busy.values.total_short_va == 4_000_000
    assert thin.values.total_short_va == 2_000_000
    assert busy.values.market_volume_va == 2 * thin.values.market_volume_va


def test_比率と絶対額は別の型で持つ():
    """型が分かれていれば、片方だけ渡して判定する事故が構造的に起きにくい。"""
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]))

    assert not hasattr(metrics.ratios, "total_short_va")
    assert not hasattr(metrics.values, "total_short_pct")


# ------------------------------------------------------------------
# 変化率（営業日ベース・当日を含めない）
# ------------------------------------------------------------------
def _five_day_history():
    # 直近5営業日の総空売り代金が 100/100/100/100/100、当日が 120
    rows = [_day(f"2026-08-1{i}", 1_000_000, 100_000) for i in range(1, 6)]
    rows.append(_day("2026-08-20", 1_000_000, 120_000))
    return _history(rows)


def test_前日比は直前の営業日を基準にする():
    metrics = build_pressure_metrics("2026-08-20", _five_day_history())
    assert metrics.short_value_change.dod_pct == pytest.approx(20.0)


def test_5日平均比の窓に当日を含めない():
    """当日を含めると自分で平均を押し上げ、極端な日ほど乖離が小さく見える。"""
    metrics = build_pressure_metrics("2026-08-20", _five_day_history())
    # 当日を含めない平均は 100,000 なので +20%。含めると +16.7% になってしまう
    assert metrics.short_value_change.vs_avg_pct == pytest.approx(20.0)


def test_日付が飛んでいても直前の行を前営業日として扱う():
    """休場をまたいでも日付の引き算をしない（1日ずれの再発防止）。"""
    rows = [
        _day("2026-08-07", 1_000_000, 100_000),  # 金
        _day("2026-08-11", 1_000_000, 150_000),  # 翌営業日（連休明け）
    ]
    metrics = build_pressure_metrics("2026-08-11", _history(rows))
    assert metrics.short_value_change.dod_pct == pytest.approx(50.0)


def test_サンプル不足なら平均比とZスコアをNoneにする():
    """欠損を補間せず「判定できない」と返す。"""
    rows = [
        _day("2026-08-27", 1_000_000, 100_000),
        _day("2026-08-28", 1_000_000, 120_000),
    ]
    metrics = build_pressure_metrics("2026-08-28", _history(rows))

    assert metrics.short_value_change.dod_pct == pytest.approx(20.0)  # 前日比は出せる
    assert metrics.short_value_change.vs_avg_pct is None              # 5日平均は無理
    assert metrics.short_value_change.zscore is None


def test_Zスコアは直近分布に対する相対位置で出す():
    """単一の固定閾値ではなく、指標自身の分布に対する位置で評価する。"""
    windows = PressureWindows(zscore_window=20, average_window=5, min_sample_coverage=0.8)
    rows = [_day(f"2026-07-{i:02d}", 1_000_000, 100_000 + (i % 2) * 2_000)
            for i in range(1, 21)]
    rows.append(_day("2026-08-01", 1_000_000, 130_000))  # 明確に外れた日

    metrics = build_pressure_metrics("2026-08-01", _history(rows), windows=windows)

    assert metrics.short_value_change.sample_size == 20
    assert metrics.short_value_change.zscore > 2.0


def test_分布が動かない系列ではZスコアを出さない():
    """標準偏差ゼロで無限大を作らない。"""
    rows = [_day(f"2026-07-{i:02d}", 1_000_000, 100_000) for i in range(1, 21)]
    rows.append(_day("2026-08-01", 1_000_000, 110_000))

    metrics = build_pressure_metrics("2026-08-01", _history(rows))
    assert metrics.short_value_change.zscore is None


def test_比率の時系列も同じ行の分母で作る():
    """比率のZスコアを作るとき、分母を別の日から持ってこない。"""
    rows = [
        _day("2026-08-26", 10_000_000, 4_000_000),  # 40%
        _day("2026-08-27", 5_000_000, 2_500_000),   # 50%
        _day("2026-08-28", 5_000_000, 2_000_000),   # 40%
    ]
    metrics = build_pressure_metrics("2026-08-28", _history(rows))

    # 空売り比率そのものは保存済みの値を使う（内訳が欠けた日でも途切れないため）
    assert metrics.total_ratio_change.latest == pytest.approx(40.0)
    assert metrics.total_ratio_change.dod_pct == pytest.approx(-20.0)  # 50% → 40%
    # 内訳由来の比率も、必ず同じ行の分母で作る（32% → 32%）
    assert metrics.with_ratio_change.latest == pytest.approx(32.0)


# ------------------------------------------------------------------
# 対象市場を混ぜない・欠損を補間しない
# ------------------------------------------------------------------
def test_騰落銘柄数はスコープ付きで受け取る():
    breadth = {
        "market_scope": "TSE_PRIME", "scope_label": "プライム",
        "advancing_issues": 873, "declining_issues": 635, "unchanged_issues": 49,
        "topix_close": 4146.71, "topix_prev_close": 4117.22, "topix_change_pct": 0.716,
    }
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]), breadth)

    assert metrics.breadth.scope == "TSE_PRIME"
    assert metrics.breadth.available is True
    assert metrics.breadth.net_breadth == pytest.approx(0.1578, abs=1e-4)
    assert metrics.price.topix_change_pct == pytest.approx(0.716)
    assert metrics.missing_inputs == ()


def test_騰落銘柄数が無い日は未取得として明示する():
    """欠損を0で埋めず、レジーム層が判定を落とせるように印を残す。"""
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]))

    assert metrics.breadth.available is False
    assert metrics.breadth.net_breadth is None
    assert metrics.price.available is False
    assert "騰落銘柄数" in metrics.missing_inputs
    assert "TOPIX騰落率" in metrics.missing_inputs
    # 空売り側の指標は問題なく出ている
    assert metrics.ratios.total_short_pct == pytest.approx(43.47, abs=0.05)


def test_TOPIXだけ取れて騰落銘柄数が欠ける場合を区別する():
    breadth = {"topix_close": 4146.71, "topix_prev_close": 4117.22,
               "topix_change_pct": 0.716}
    metrics = build_pressure_metrics("2026-08-28", _history([REAL_DAY]), breadth)

    assert metrics.price.available is True
    assert metrics.breadth.available is False
    assert metrics.missing_inputs == ("騰落銘柄数",)


def test_対象日のデータが無ければ指標を組み立てない():
    metrics = build_pressure_metrics("2026-08-31", _history([REAL_DAY]))
    assert metrics.ratios.total_short_pct is None
    assert metrics.missing_inputs == ("空売り集計",)


def test_対象日より後のデータは使わない():
    """未来の行が混ざっていても当日として拾わない。"""
    rows = [REAL_DAY, _day("2026-08-31", 1_000_000, 900_000)]
    metrics = build_pressure_metrics("2026-08-28", _history(rows))
    assert metrics.ratios.total_short_pct == pytest.approx(43.47, abs=0.05)


# ------------------------------------------------------------------
# 単位
# ------------------------------------------------------------------
def test_百万円を兆円へ換算する():
    """JPX PDF の【単位：百万円】に基づく。9,093,361百万円 = 9.09兆円。"""
    assert to_trillion_yen(9_093_361.0) == pytest.approx(9.093, abs=0.001)
    assert format_trillion_yen(9_093_361.0) == "9.09兆円"
    assert format_trillion_yen(None) == "—"
