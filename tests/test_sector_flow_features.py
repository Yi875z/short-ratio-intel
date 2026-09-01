"""
業種別フロー特徴量の算出テスト（ネットワーク・DB非依存）。

Phase 0 の約束を回帰として固定する:
  - 同一日内の指標は生値、騰落率は調整後終値（調整を混ぜない）
  - 時価総額加重の重みは前日の時価総額（当日の値動きを重みに含めない）
  - 欠損は補間せず、分母から外す
  - 母集団は普通株の主要3市場のみ（ETF・REIT・PROを含めない）
  - 将来リターンは先の営業日が足りなければ None（打ち切らない）
"""
import pytest

from src.analyzer.sector_flow_features import (
    SECTOR_UNIVERSE_SCOPE,
    compute_forward_returns,
    compute_sector_features,
)


def _master(*rows):
    """(code, s33, mkt) から銘柄一覧を作る。"""
    return [{"Code": c, "S33": s, "Mkt": m} for c, s, m in rows]


def _bar(code, close, *, open_=None, high=None, low=None, volume=1000.0,
         turnover=None, adj_close=None, market_cap=1000.0):
    """既定では VWAP = close（turnover = close * volume）。"""
    return {
        "Code": code,
        "O": close if open_ is None else open_,
        "H": close if high is None else high,
        "L": close if low is None else low,
        "C": close,
        "Vo": volume,
        "Va": close * volume if turnover is None else turnover,
        "AdjC": close if adj_close is None else adj_close,
        "MktCap": market_cap,
    }


def _only(features, s33="3650"):
    return next(f for f in features if f.s33_code == s33)


# ------------------------------------------------------------------
# 母集団
# ------------------------------------------------------------------
def test_普通株の主要3市場だけを業種の母集団にする():
    """ETF・REIT（0109）と PRO（0105）は業種の値動きを歪めるため除く。"""
    master = _master(
        ("1000", "3650", "0111"), ("2000", "3650", "0112"), ("3000", "3650", "0113"),
        ("4000", "3650", "0109"), ("5000", "3650", "0105"),
    )
    bars = [_bar(c, 100) for c in ("1000", "2000", "3000", "4000", "5000")]

    features = _only(compute_sector_features("2026-08-28", bars, bars, master))

    assert features.constituents == 3
    assert features.scope == SECTOR_UNIVERSE_SCOPE


def test_業種コードが無い銘柄は除外する():
    master = [{"Code": "1000", "S33": "3650", "Mkt": "0111"},
              {"Code": "2000", "S33": None, "Mkt": "0111"}]
    bars = [_bar("1000", 100), _bar("2000", 100)]

    features = compute_sector_features("2026-08-28", bars, bars, master)
    assert len(features) == 1
    assert features[0].constituents == 1


def test_業種ごとに別々の行を返す():
    master = _master(("1000", "3650", "0111"), ("2000", "3200", "0111"))
    bars = [_bar("1000", 110), _bar("2000", 90)]
    prev = [_bar("1000", 100), _bar("2000", 100)]

    features = compute_sector_features("2026-08-28", bars, prev, master)

    assert {f.s33_code for f in features} == {"3650", "3200"}
    assert _only(features, "3650").ret_equal_weighted == pytest.approx(10.0)
    assert _only(features, "3200").ret_equal_weighted == pytest.approx(-10.0)


# ------------------------------------------------------------------
# 騰落率と調整の扱い
# ------------------------------------------------------------------
def test_騰落率は調整後終値どうしで計算する():
    """分割日に生値で比べると暴落と誤認する。"""
    master = _master(("1000", "3650", "0111"))
    # 1:5分割: 生値 27,600 → 5,200 だが調整後は 5,520 → 5,200
    today = [_bar("1000", 5200.0, adj_close=5200.0)]
    prev = [_bar("1000", 27600.0, adj_close=5520.0)]

    features = _only(compute_sector_features("2026-08-28", today, prev, master))

    assert features.ret_equal_weighted == pytest.approx(-5.797, abs=0.01)


def test_時価総額加重の重みは前日の時価総額を使う():
    """当日の値動きを重みに含めると、上げた銘柄の影響が二重に効く。"""
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [_bar("1000", 110, market_cap=9999.0), _bar("2000", 90, market_cap=1.0)]
    prev = [_bar("1000", 100, market_cap=100.0), _bar("2000", 100, market_cap=300.0)]

    features = _only(compute_sector_features("2026-08-28", today, prev, master))

    # 前日の時価総額 100:300 で加重 → (+10*100 - 10*300) / 400 = -5.0
    assert features.ret_cap_weighted == pytest.approx(-5.0)
    # 単純平均なら 0.0 になるので、加重が効いていることが分かる
    assert features.ret_equal_weighted == pytest.approx(0.0)


def test_対TOPIX相対はTOPIXが無ければNone():
    master = _master(("1000", "3650", "0111"))
    today, prev = [_bar("1000", 110)], [_bar("1000", 100)]

    with_topix = _only(compute_sector_features("2026-08-28", today, prev, master, 3.0))
    without = _only(compute_sector_features("2026-08-28", today, prev, master))

    assert with_topix.excess_ret_vs_topix == pytest.approx(7.0)
    assert without.excess_ret_vs_topix is None


# ------------------------------------------------------------------
# VWAP・終値位置（同一日内なので生値）
# ------------------------------------------------------------------
def test_VWAPは売買代金を出来高で割って求める():
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    # 1000: 終値105 > VWAP100 / 2000: 終値95 < VWAP100
    today = [
        _bar("1000", 105, volume=1000.0, turnover=100_000.0),
        _bar("2000", 95, volume=1000.0, turnover=100_000.0),
    ]

    features = _only(compute_sector_features("2026-08-28", today, today, master))
    assert features.above_vwap_pct == pytest.approx(50.0)


def test_終値位置と高値圏引け比率を計算する():
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [
        _bar("1000", 100, high=100, low=90),   # (100-90)/(100-90) = 1.0 高値引け
        _bar("2000", 90, high=100, low=90),    # 0.0 安値引け
    ]

    features = _only(compute_sector_features("2026-08-28", today, today, master))

    assert features.high_close_pct == pytest.approx(50.0)
    assert features.close_location_median == pytest.approx(0.5)


def test_値幅ゼロの銘柄は終値位置の分母から外す():
    """ストップ高安や無風の日にゼロ除算しない。"""
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [
        _bar("1000", 100, high=100, low=100),   # 値幅ゼロ
        _bar("2000", 100, high=100, low=90),    # 1.0
    ]

    features = _only(compute_sector_features("2026-08-28", today, today, master))
    assert features.high_close_pct == pytest.approx(100.0)  # 判定できた1件のうち1件


def test_終値が始値を上回った比率を別に持つ():
    """終値位置だけでは「安値から回復」と「寄りから強い」を区別できない。

    日足しか無い以上そこは原理的に分離できないため、始値比を併記して
    読み手が切り分けられるようにする。
    """
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [
        _bar("1000", 100, open_=90, high=100, low=88),   # 寄りから上げて高値引け
        _bar("2000", 100, open_=110, high=115, low=95),  # 寄り天だが安値からは戻した
    ]

    features = _only(compute_sector_features("2026-08-28", today, today, master))
    assert features.close_above_open_pct == pytest.approx(50.0)


# ------------------------------------------------------------------
# 欠損の扱い
# ------------------------------------------------------------------
def test_前日の足が無い銘柄は騰落の分母から外す():
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [_bar("1000", 110), _bar("2000", 110)]
    prev = [_bar("1000", 100)]  # 2000 は新規上場

    features = _only(compute_sector_features("2026-08-28", today, prev, master))

    assert features.constituents == 2
    assert features.compared == 1
    assert features.advancing_pct == pytest.approx(100.0)  # 判定できた1件が上昇


def test_当日の足が無い銘柄でも構成銘柄数には数える():
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [_bar("1000", 110)]
    prev = [_bar("1000", 100), _bar("2000", 100)]

    features = _only(compute_sector_features("2026-08-28", today, prev, master))
    assert features.constituents == 2
    assert features.compared == 1


def test_全滅していれば比率はNoneにする():
    master = _master(("1000", "3650", "0111"))
    features = _only(compute_sector_features("2026-08-28", [], [], master))

    assert features.constituents == 1
    assert features.compared == 0
    assert features.above_vwap_pct is None
    assert features.ret_cap_weighted is None


# ------------------------------------------------------------------
# 売買代金上位バスケット
# ------------------------------------------------------------------
def test_売買代金上位Nとそのシェアを算出する():
    master = _master(*[(f"{i}000", "3650", "0111") for i in range(1, 6)])
    today = [
        _bar("1000", 100, volume=1000.0, turnover=500.0),
        _bar("2000", 100, volume=1000.0, turnover=300.0),
        _bar("3000", 100, volume=1000.0, turnover=100.0),
        _bar("4000", 100, volume=1000.0, turnover=60.0),
        _bar("5000", 100, volume=1000.0, turnover=40.0),
    ]

    features = _only(compute_sector_features("2026-08-28", today, today, master, top_n=2))

    assert features.turnover_total == pytest.approx(1000.0)
    assert features.top_n_codes == ("1000", "2000")
    assert features.top_n_turnover_share == pytest.approx(80.0)


def test_上位バスケットの状態を件数で持つ():
    master = _master(("1000", "3650", "0111"), ("2000", "3650", "0111"))
    today = [
        # VWAP=100、終値105で高値引け
        _bar("1000", 105, high=105, low=95, volume=1000.0, turnover=100_000.0),
        # VWAP=100、終値95で安値引け
        _bar("2000", 95, high=105, low=95, volume=1000.0, turnover=100_000.0),
    ]
    prev = [_bar("1000", 100), _bar("2000", 100)]

    features = _only(compute_sector_features("2026-08-28", today, prev, master, top_n=2))

    assert features.top_n_above_vwap == 1
    assert features.top_n_high_close == 1
    assert features.top_n_advancing == 1


# ------------------------------------------------------------------
# 将来リターン（検証専用）
# ------------------------------------------------------------------
def _rows(*returns):
    return [
        {"date": f"2026-08-{i + 1:02d}", "s33_code": "3650",
         "ret_cap_weighted": r, "excess_ret_vs_topix": r / 2}
        for i, r in enumerate(returns)
    ]


def test_将来リターンは翌営業日から複利で積む():
    result = compute_forward_returns(_rows(1.0, 2.0, 3.0, 4.0), horizons=(1, 3))

    first = result["2026-08-01|3650"]
    assert first["fwd_ret_1d"] == pytest.approx(2.0)
    # 1.02 * 1.03 * 1.04 - 1 = 9.2624%
    assert first["fwd_ret_3d"] == pytest.approx(9.2624, abs=0.001)


def test_超過リターンは単純合計で積む():
    """差分は複利にしない（対TOPIXの累積は加算が素直）。"""
    result = compute_forward_returns(_rows(1.0, 2.0, 3.0, 4.0), horizons=(3,))
    # 1.0 + 1.5 + 2.0
    assert result["2026-08-01|3650"]["fwd_excess_3d"] == pytest.approx(4.5)


def test_先の営業日が足りない行はNoneのままにする():
    """直近の行は後日データが増えてから埋まる。打ち切らない。"""
    result = compute_forward_returns(_rows(1.0, 2.0), horizons=(1, 3))

    last = result["2026-08-02|3650"]
    assert last["fwd_ret_1d"] is None
    assert last["fwd_ret_3d"] is None


def test_途中に欠損があればその窓はNoneにする():
    rows = _rows(1.0, 2.0, 3.0)
    rows[1]["ret_cap_weighted"] = None

    result = compute_forward_returns(rows, horizons=(1, 2))
    assert result["2026-08-01|3650"]["fwd_ret_1d"] is None
    assert result["2026-08-01|3650"]["fwd_ret_2d"] is None


def test_業種ごとに独立して計算する():
    rows = [
        {"date": "2026-08-01", "s33_code": "3650", "ret_cap_weighted": 1.0,
         "excess_ret_vs_topix": 0.5},
        {"date": "2026-08-02", "s33_code": "3650", "ret_cap_weighted": 2.0,
         "excess_ret_vs_topix": 1.0},
        {"date": "2026-08-01", "s33_code": "3200", "ret_cap_weighted": -1.0,
         "excess_ret_vs_topix": -0.5},
        {"date": "2026-08-02", "s33_code": "3200", "ret_cap_weighted": -2.0,
         "excess_ret_vs_topix": -1.0},
    ]
    result = compute_forward_returns(rows, horizons=(1,))

    assert result["2026-08-01|3650"]["fwd_ret_1d"] == pytest.approx(2.0)
    assert result["2026-08-01|3200"]["fwd_ret_1d"] == pytest.approx(-2.0)


def test_NaNは欠損として扱い伝播させない():
    """pandas 経由で読むと None は NaN になる。NaN を通すと計算結果まで NaN になり、
    そのまま DB へ書き込まれて欠損が静かに汚染される（2026-09-01 の実障害）。
    """
    rows = _rows(1.0, 2.0, 3.0)
    rows[1]["ret_cap_weighted"] = float("nan")

    result = compute_forward_returns(rows, horizons=(1, 2))

    assert result["2026-08-01|3650"]["fwd_ret_1d"] is None
    assert result["2026-08-01|3650"]["fwd_ret_2d"] is None


def test_日付順が崩れていても並べ替えて計算する():
    rows = list(reversed(_rows(1.0, 2.0, 3.0)))
    result = compute_forward_returns(rows, horizons=(1,))
    assert result["2026-08-01|3650"]["fwd_ret_1d"] == pytest.approx(2.0)
