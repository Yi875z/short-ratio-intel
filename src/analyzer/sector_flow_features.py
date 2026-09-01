"""
業種別フロー特徴量の算出（Phase 0: 保存のみ。判定はしない）。

## この層の目的

「大量の空売りフローが出た業種で、市場はその売りをどう処理したのか」を測るための
特徴量を、毎日・業種ごとに1行として保存する。**この層は状態分類を一切行わない。**

理由は、既存システムが既に1日63件の判定（異常値20・機械判定シグナル8・業種行34・
市場レジーム1）を出している一方で、**そのどれ一つとして「翌日の値動きと関係があったか」
を検証する仕組みが無い**ため。判定を増やす前に、特徴量と将来リターンを同じ行に並べて
測れる状態を作る。状態分類は、効く特徴量が判明してから最小限だけ作る。

## 母集団を混ぜないための約束

JPX空売り集計の業種別は東証全体（外国株券等を含む）ベース、こちらは
J-Quants の S33 に基づくプライム・スタンダード・グロースの普通株。**対象範囲が違う。**
したがってこのテーブルの特徴量と空売り比率を**掛け合わせない**。
join して並べて見るだけにする。scope 列と constituents 列がその境界を示す。

## 調整の扱い（重要）

- **騰落率は調整後終値 `AdjC` 同士**で計算する（分割・併合をまたぐため）
- **VWAP位置と終値位置は同一日内の生値**で計算する（`C` vs `Va/Vo`、`O/H/L/C`）
  同じ日の中の比較なので調整は不要であり、むしろ調整値を混ぜると分母がずれる
- 時価総額加重は**前日の時価総額**を重みにする（当日の値動きを重みに含めない）

## 「回復」と「最初から強い」を分けるために

`close_location = (C-L)/(H-L)` は 0.9 でも「安値から戻した」と「寄りから一貫して高い」を
区別できない。日足しか無い以上ここは原理的に分離できないため、
`close_above_open_pct`（終値>始値の銘柄比率）を併記して、
読み手が両者を切り分けられるようにする。日中足が取れないことの明示的な代償。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

# 業種の母集団に含める市場区分（普通株の主要3市場）。
# ETF・REIT（0109）と TOKYO PRO MARKET（0105）は業種の値動きを歪めるため除く。
SECTOR_UNIVERSE_MARKETS = ("0111", "0112", "0113")
SECTOR_UNIVERSE_SCOPE = "TSE_PRIME_STANDARD_GROWTH"

# 終値位置がこれ以上なら「高値圏引け」とみなす（仕様書 §5-4 の 0.75 を採用）。
HIGH_CLOSE_LOCATION = 0.75

# 売買代金上位バスケットの既定サイズ。
DEFAULT_TOP_N = 10

FEATURE_SOURCE = "JQUANTS_V2"


@dataclass
class _Constituent:
    """業種構成銘柄1件ぶんの中間計算値。"""

    code: str
    ret: Optional[float] = None                 # AdjC ベースの前日比（比率）
    turnover: Optional[float] = None            # Va（円）
    prev_market_cap: Optional[float] = None     # 前日の時価総額（百万円）
    above_vwap: Optional[bool] = None           # C > Va/Vo
    close_location: Optional[float] = None      # (C-L)/(H-L)
    close_above_open: Optional[bool] = None     # C > O


@dataclass(frozen=True)
class SectorFlowFeatures:
    """1営業日・1業種ぶんの特徴量。将来リターンは後から埋める（初期は None）。"""

    date: str
    s33_code: str
    scope: str = SECTOR_UNIVERSE_SCOPE

    # 母集団
    constituents: int = 0            # 業種に属する銘柄数
    compared: int = 0                # 前日比を計算できた銘柄数（補間しない）

    # 値動き
    ret_cap_weighted: Optional[float] = None    # 前日時価総額加重の業種騰落率(%)
    ret_equal_weighted: Optional[float] = None  # 単純平均の業種騰落率(%)
    excess_ret_vs_topix: Optional[float] = None # 対TOPIX相対(pt)

    # 市場がどう処理したか（Breadth）
    above_vwap_pct: Optional[float] = None      # 終値が当日VWAP超の銘柄比率(%)
    high_close_pct: Optional[float] = None      # 終値位置>=0.75 の銘柄比率(%)
    advancing_pct: Optional[float] = None       # 前日比プラスの銘柄比率(%)
    close_above_open_pct: Optional[float] = None  # 終値>始値の銘柄比率(%)
    close_location_median: Optional[float] = None  # 業種の終値位置の中央値

    # 売買代金と上位バスケット
    turnover_total: Optional[float] = None      # 業種の売買代金合計（円）
    top_n: int = DEFAULT_TOP_N
    top_n_turnover_share: Optional[float] = None  # 上位N銘柄が占める代金比率(%)
    top_n_above_vwap: Optional[int] = None        # 上位Nのうち VWAP超の銘柄数
    top_n_high_close: Optional[int] = None        # 上位Nのうち高値圏引けの銘柄数
    top_n_advancing: Optional[int] = None         # 上位Nのうち上昇した銘柄数
    top_n_codes: tuple[str, ...] = ()             # 監査用（上位Nの銘柄コード）

    # 将来リターン（別パスで後から埋める。判定には使わない・保存のみ）
    fwd_ret_1d: Optional[float] = None
    fwd_ret_3d: Optional[float] = None
    fwd_ret_5d: Optional[float] = None
    fwd_excess_1d: Optional[float] = None
    fwd_excess_3d: Optional[float] = None
    fwd_excess_5d: Optional[float] = None

    source: str = FEATURE_SOURCE

    def to_dict(self) -> dict:
        data = asdict(self)
        data["top_n_codes"] = list(self.top_n_codes)
        return data


# ----------------------------------------------------------------------
# 算出（純粋関数）
# ----------------------------------------------------------------------
def compute_sector_features(
    target_date: str,
    bars_today: Iterable[dict],
    bars_prev: Iterable[dict],
    master: Iterable[dict],
    topix_change_pct: Optional[float] = None,
    top_n: int = DEFAULT_TOP_N,
) -> list[SectorFlowFeatures]:
    """業種ごとの特徴量を返す。

    Args:
        target_date:      対象営業日
        bars_today:       対象日の全銘柄日足
        bars_prev:        前営業日の全銘柄日足
        master:           対象日時点の上場銘柄一覧（S33 と Mkt を含む）
        topix_change_pct: TOPIX の当日騰落率(%)。無ければ相対リターンは None
        top_n:            売買代金上位バスケットのサイズ
    """
    today_by_code = {row["Code"]: row for row in bars_today if row.get("Code")}
    prev_by_code = {row["Code"]: row for row in bars_prev if row.get("Code")}

    by_sector: dict[str, list[_Constituent]] = {}
    for issue in master:
        if issue.get("Mkt") not in SECTOR_UNIVERSE_MARKETS:
            continue
        s33 = issue.get("S33")
        if not s33:
            continue
        code = issue.get("Code")
        by_sector.setdefault(s33, []).append(
            _build_constituent(code, today_by_code.get(code), prev_by_code.get(code))
        )

    return [
        _aggregate(target_date, s33, members, topix_change_pct, top_n)
        for s33, members in sorted(by_sector.items())
    ]


def _build_constituent(
    code: str, today_row: Optional[dict], prev_row: Optional[dict]
) -> _Constituent:
    member = _Constituent(code=code)
    if not today_row:
        return member

    # --- 同一日内の指標は生値で見る（調整値を混ぜない） ---
    open_ = _as_float(today_row.get("O"))
    high = _as_float(today_row.get("H"))
    low = _as_float(today_row.get("L"))
    close = _as_float(today_row.get("C"))
    volume = _as_float(today_row.get("Vo"))
    turnover = _as_float(today_row.get("Va"))

    member.turnover = turnover

    if close is not None and volume and turnover:
        vwap = turnover / volume
        member.above_vwap = close > vwap

    if close is not None and high is not None and low is not None and high > low:
        member.close_location = round((close - low) / (high - low), 4)

    if close is not None and open_ is not None:
        member.close_above_open = close > open_

    # --- 騰落率は調整後終値どうし（分割・併合をまたぐため） ---
    if prev_row:
        adj_close = _as_float(today_row.get("AdjC")) or close
        prev_adj_close = _as_float(prev_row.get("AdjC")) or _as_float(prev_row.get("C"))
        if adj_close is not None and prev_adj_close:
            member.ret = adj_close / prev_adj_close - 1.0
        # 重みは前日の時価総額（当日の値動きを重みに含めない）
        member.prev_market_cap = _as_float(prev_row.get("MktCap"))

    return member


def _aggregate(
    target_date: str,
    s33_code: str,
    members: list[_Constituent],
    topix_change_pct: Optional[float],
    top_n: int,
) -> SectorFlowFeatures:
    compared = [m for m in members if m.ret is not None]

    ret_equal = _mean([m.ret for m in compared])
    ret_cap = _weighted_mean(
        [(m.ret, m.prev_market_cap) for m in compared if m.prev_market_cap]
    )
    ret_cap_pct = None if ret_cap is None else round(ret_cap * 100, 4)
    ret_equal_pct = None if ret_equal is None else round(ret_equal * 100, 4)

    excess = None
    if ret_cap_pct is not None and topix_change_pct is not None:
        excess = round(ret_cap_pct - topix_change_pct, 4)

    with_turnover = sorted(
        (m for m in members if m.turnover),
        key=lambda m: m.turnover,
        reverse=True,
    )
    turnover_total = sum(m.turnover for m in with_turnover) or None
    top_members = with_turnover[:top_n]
    top_turnover = sum(m.turnover for m in top_members)

    return SectorFlowFeatures(
        date=target_date,
        s33_code=s33_code,
        constituents=len(members),
        compared=len(compared),
        ret_cap_weighted=ret_cap_pct,
        ret_equal_weighted=ret_equal_pct,
        excess_ret_vs_topix=excess,
        above_vwap_pct=_ratio_pct([m.above_vwap for m in members]),
        high_close_pct=_ratio_pct([
            None if m.close_location is None else m.close_location >= HIGH_CLOSE_LOCATION
            for m in members
        ]),
        advancing_pct=_ratio_pct([
            None if m.ret is None else m.ret > 0 for m in members
        ]),
        close_above_open_pct=_ratio_pct([m.close_above_open for m in members]),
        close_location_median=_median(
            [m.close_location for m in members if m.close_location is not None]
        ),
        turnover_total=turnover_total,
        top_n=top_n,
        top_n_turnover_share=(
            round(top_turnover / turnover_total * 100, 2) if turnover_total else None
        ),
        top_n_above_vwap=_count_true([m.above_vwap for m in top_members]),
        top_n_high_close=_count_true([
            None if m.close_location is None else m.close_location >= HIGH_CLOSE_LOCATION
            for m in top_members
        ]),
        top_n_advancing=_count_true([
            None if m.ret is None else m.ret > 0 for m in top_members
        ]),
        top_n_codes=tuple(m.code for m in top_members),
    )


# ----------------------------------------------------------------------
# 将来リターン（判定には使わない。検証のために保存するだけ）
# ----------------------------------------------------------------------
def compute_forward_returns(
    rows: list[dict],
    horizons: tuple[int, ...] = (1, 3, 5),
) -> dict[str, dict[str, Optional[float]]]:
    """業種ごとの日次リターン列から、各日の N営業日先までの累積リターンを返す。

    Args:
        rows: {"date", "s33_code", "ret_cap_weighted", "excess_ret_vs_topix"} を持つ行。
              業種混在でよい（内部で業種ごとに分けて日付順に処理する）。

    Returns:
        {f"{date}|{s33_code}": {"fwd_ret_1d": ..., "fwd_excess_1d": ..., ...}}

    先の営業日が足りない直近の行は None のままにする（補間も打ち切りもしない）。
    後日データが増えたときに再実行すれば埋まる。
    """
    by_sector: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("s33_code") and row.get("date"):
            by_sector.setdefault(row["s33_code"], []).append(row)

    result: dict[str, dict[str, Optional[float]]] = {}
    for s33_code, sector_rows in by_sector.items():
        ordered = sorted(sector_rows, key=lambda r: r["date"])
        returns = [_as_float(r.get("ret_cap_weighted")) for r in ordered]
        excess = [_as_float(r.get("excess_ret_vs_topix")) for r in ordered]

        for index, row in enumerate(ordered):
            values: dict[str, Optional[float]] = {}
            for horizon in horizons:
                values[f"fwd_ret_{horizon}d"] = _cumulative(returns, index, horizon)
                values[f"fwd_excess_{horizon}d"] = _cumulative_sum(excess, index, horizon)
            result[f"{row['date']}|{s33_code}"] = values

    return result


def _cumulative(series: list[Optional[float]], index: int, horizon: int) -> Optional[float]:
    """index の翌営業日から horizon 日ぶんの累積リターン(%)。複利で積む。"""
    window = series[index + 1: index + 1 + horizon]
    if len(window) < horizon or any(v is None for v in window):
        return None
    compounded = 1.0
    for value in window:
        compounded *= 1.0 + value / 100.0
    return round((compounded - 1.0) * 100, 4)


def _cumulative_sum(series: list[Optional[float]], index: int, horizon: int) -> Optional[float]:
    """超過リターンは差分なので単純合計で積む（複利にしない）。"""
    window = series[index + 1: index + 1 + horizon]
    if len(window) < horizon or any(v is None for v in window):
        return None
    return round(sum(window), 4)


# ----------------------------------------------------------------------
def _ratio_pct(flags: list[Optional[bool]]) -> Optional[float]:
    """True の比率(%)。判定できなかった銘柄は分母から外す（補間しない）。"""
    known = [f for f in flags if f is not None]
    if not known:
        return None
    return round(sum(1 for f in known if f) / len(known) * 100, 2)


def _count_true(flags: list[Optional[bool]]) -> Optional[int]:
    known = [f for f in flags if f is not None]
    if not known:
        return None
    return sum(1 for f in known if f)


def _mean(values: list[Optional[float]]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 4)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 4)


def _weighted_mean(pairs: list[tuple[Optional[float], Optional[float]]]) -> Optional[float]:
    total_weight = 0.0
    total = 0.0
    for value, weight in pairs:
        if value is None or not weight:
            continue
        total += value * weight
        total_weight += weight
    return total / total_weight if total_weight else None


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
