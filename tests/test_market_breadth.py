"""
騰落銘柄数・TOPIX騰落率の算出テスト（ネットワーク非依存）。

固定値は 2026-08-28 の実データから取っている。特に株式分割銘柄の扱いは、
生値で比べると誤カウントする実例をそのまま回帰テストにしてある。
"""
import pytest

from src.analyzer.market_breadth import (
    BreadthCounts,
    compute_all_breadth,
    compute_breadth,
    compute_topix_change,
    previous_business_day,
)
from src.data_fetcher.jquants_api_client import (
    MARKET_CODE_GROWTH,
    MARKET_CODE_PRIME,
    MARKET_CODE_STANDARD,
)


def _master(*rows):
    return [{"Code": code, "Mkt": mkt} for code, mkt in rows]


def _bar(code, close, adj_close=None, adj_factor=1.0):
    return {
        "Code": code,
        "C": close,
        "AdjC": close if adj_close is None else adj_close,
        "AdjFactor": adj_factor,
    }


# ----------------------------------------------------------------------
# 基本の数え方
# ----------------------------------------------------------------------
def test_値上がり値下がり変わらずを数える():
    master = _master(("1000", MARKET_CODE_PRIME), ("2000", MARKET_CODE_PRIME),
                     ("3000", MARKET_CODE_PRIME))
    today = [_bar("1000", 110), _bar("2000", 90), _bar("3000", 100)]
    prev = [_bar("1000", 100), _bar("2000", 100), _bar("3000", 100)]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)

    assert (counts.advancing, counts.declining, counts.unchanged) == (1, 1, 1)
    assert counts.universe == 3
    assert counts.compared == 3
    assert counts.not_compared == 0
    assert counts.scope == "TSE_PRIME"
    assert counts.scope_label == "プライム"


def test_ネットブレッドスと騰落レシオを算出する():
    counts = BreadthCounts(
        date="2026-08-28", scope="TSE_PRIME", scope_label="プライム",
        advancing=873, declining=635, unchanged=49, not_compared=0, universe=1557,
    )
    # (873 - 635) / (873 + 635)
    assert counts.net_breadth == pytest.approx(0.1578, abs=1e-4)
    assert counts.advance_decline_ratio == pytest.approx(1.3748, abs=1e-4)


def test_値下がりゼロなら騰落レシオはNoneにする():
    """無限大を大きな数値で誤魔化すと、そのまま閾値判定に流れて事故になる。"""
    counts = BreadthCounts(
        date="2026-08-28", scope="TSE_PRIME", scope_label="プライム",
        advancing=10, declining=0, unchanged=0, not_compared=0, universe=10,
    )
    assert counts.advance_decline_ratio is None
    assert counts.net_breadth == 1.0


def test_判定できる銘柄が無ければネットブレッドスはNone():
    counts = BreadthCounts(
        date="2026-08-28", scope="TSE_PRO", scope_label="PRO",
        advancing=0, declining=0, unchanged=0, not_compared=187, universe=187,
    )
    assert counts.net_breadth is None


# ----------------------------------------------------------------------
# 分割・併合（実データ由来の回帰）
# ----------------------------------------------------------------------
def test_分割銘柄は調整後終値で比較する():
    """2026-08-28 の 68340（1:5分割）の実値。

    生値は 27,600 → 5,200 で「大暴落」に見えるが、調整後は 5,520 → 5,200 の
    小幅安。AdjC 同士で比較しないと値下がり判定が壊れる。
    """
    master = _master(("68340", MARKET_CODE_PRIME))
    today = [_bar("68340", close=5200.0, adj_close=5200.0, adj_factor=0.2)]
    prev = [_bar("68340", close=27600.0, adj_close=5520.0)]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)

    assert counts.declining == 1
    assert counts.advancing == 0


def test_分割で値上がりしている銘柄を値下がりと誤判定しない():
    master = _master(("99990", MARKET_CODE_PRIME))
    # 1:2分割かつ実質上昇: 前日 2,000（調整後1,000）→ 当日 1,050
    today = [_bar("99990", close=1050.0, adj_close=1050.0, adj_factor=0.5)]
    prev = [_bar("99990", close=2000.0, adj_close=1000.0)]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)

    assert counts.advancing == 1
    assert counts.declining == 0


def test_調整後終値が無ければ生の終値で比較する():
    master = _master(("1000", MARKET_CODE_PRIME))
    today = [{"Code": "1000", "C": 110}]
    prev = [{"Code": "1000", "C": 100}]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)
    assert counts.advancing == 1


# ----------------------------------------------------------------------
# 欠損の扱い（補間しない）
# ----------------------------------------------------------------------
def test_前日の足が無い銘柄は判定不能に積んで補間しない():
    master = _master(("1000", MARKET_CODE_PRIME), ("2000", MARKET_CODE_PRIME))
    today = [_bar("1000", 110), _bar("2000", 100)]
    prev = [_bar("1000", 100)]  # 2000 は新規上場などで前日の足が無い

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)

    assert counts.advancing == 1
    assert counts.not_compared == 1
    assert counts.universe == 2
    assert counts.compared == 1


def test_終値がNoneの銘柄は判定不能にする():
    master = _master(("1000", MARKET_CODE_PRIME))
    today = [{"Code": "1000", "C": None, "AdjC": None}]
    prev = [_bar("1000", 100)]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)
    assert counts.not_compared == 1
    assert counts.compared == 0


# ----------------------------------------------------------------------
# スコープの分離（混ぜない）
# ----------------------------------------------------------------------
def test_母集団は対象日時点の銘柄一覧で決まる():
    """日足に居ても銘柄一覧に無い銘柄は数えない（母集団のずれを防ぐ）。"""
    master = _master(("1000", MARKET_CODE_PRIME))
    today = [_bar("1000", 110), _bar("9999", 110)]
    prev = [_bar("1000", 100), _bar("9999", 100)]

    counts = compute_breadth("2026-08-28", today, prev, master, MARKET_CODE_PRIME)
    assert counts.universe == 1
    assert counts.advancing == 1


def test_市場区分ごとに別々のスコープで数える():
    master = _master(
        ("1000", MARKET_CODE_PRIME),
        ("2000", MARKET_CODE_STANDARD),
        ("3000", MARKET_CODE_GROWTH),
    )
    today = [_bar("1000", 110), _bar("2000", 90), _bar("3000", 110)]
    prev = [_bar("1000", 100), _bar("2000", 100), _bar("3000", 100)]

    result = compute_all_breadth("2026-08-28", today, prev, master)

    assert result["TSE_PRIME"].advancing == 1
    assert result["TSE_PRIME"].declining == 0
    assert result["TSE_STANDARD"].declining == 1
    assert result["TSE_GROWTH"].advancing == 1
    # 各スコープは自分の母集団だけを見る（他区分の銘柄を数え込まない）
    for scope in ("TSE_PRIME", "TSE_STANDARD", "TSE_GROWTH"):
        assert result[scope].universe == 1
    # 該当銘柄が無い区分は0件で返り、他区分から借りてこない
    assert result["TSE_OTHER"].universe == 0
    assert result["TSE_OTHER"].compared == 0
    assert result["TSE_OTHER"].net_breadth is None


# ----------------------------------------------------------------------
# TOPIX
# ----------------------------------------------------------------------
TOPIX_BARS = [
    {"Date": "2026-08-25", "O": 4073.89, "H": 4096.05, "L": 4060.97, "C": 4093.67},
    {"Date": "2026-08-26", "O": 4096.35, "H": 4118.44, "L": 4090.39, "C": 4111.02},
    {"Date": "2026-08-27", "O": 4124.59, "H": 4130.91, "L": 4100.10, "C": 4105.00},
]


def test_TOPIXの騰落率は直前の足を前日として計算する():
    change = compute_topix_change(TOPIX_BARS, "2026-08-26")

    assert change is not None
    assert change.close == 4111.02
    assert change.prev_close == 4093.67
    assert change.change == pytest.approx(17.35, abs=0.01)
    assert change.change_pct == pytest.approx(0.424, abs=0.001)


def test_TOPIXは並び順が崩れていても日付順で解決する():
    shuffled = [TOPIX_BARS[2], TOPIX_BARS[0], TOPIX_BARS[1]]
    change = compute_topix_change(shuffled, "2026-08-27")
    assert change.prev_close == 4111.02


def test_TOPIXの先頭日は前日が無いので騰落率をNoneにする():
    change = compute_topix_change(TOPIX_BARS, "2026-08-25")
    assert change.close == 4093.67
    assert change.prev_close is None
    assert change.change_pct is None


def test_対象日の足が無ければNoneを返す():
    assert compute_topix_change(TOPIX_BARS, "2026-08-28") is None


# ----------------------------------------------------------------------
# 取引カレンダー
# ----------------------------------------------------------------------
CALENDAR = [
    {"Date": "2026-08-27", "HolDiv": "1"},
    {"Date": "2026-08-28", "HolDiv": "1"},
    {"Date": "2026-08-29", "HolDiv": "0"},
    {"Date": "2026-08-30", "HolDiv": "0"},
    {"Date": "2026-08-31", "HolDiv": "1"},
]


def test_前営業日は公式カレンダーで決める():
    assert previous_business_day(CALENDAR, "2026-08-28") == "2026-08-27"
    # 週末をまたいでも1日ずつ遡らずに前営業日へ着地する
    assert previous_business_day(CALENDAR, "2026-08-31") == "2026-08-28"


def test_前営業日が範囲内に無ければNone():
    assert previous_business_day(CALENDAR, "2026-08-27") is None
