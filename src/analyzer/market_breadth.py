"""
騰落銘柄数（ブレッドス）と TOPIX 騰落率の算出。

J-Quants API v2（Light 以上）の全銘柄日足と上場銘柄一覧から、市場区分ごとの
値上がり・値下がり・変わらず銘柄数を自前で数える。日経等の公表ページを
スクレイプせずに済み、公式データなので過去分のバックフィルもできる。

## 算出ルール（推測せず固定する）

1. 母集団は **対象日時点**の上場銘柄一覧（/equities/master?date=対象日）。
   省略すると翌営業日時点の一覧が返り、新規上場・市場変更のぶん母集団がずれる。
2. 騰落判定は **AdjC（調整後終値）同士**の比較。分割・併合の当日でも前日側の
   AdjC が調整済みで返るため、生値 C で比べると誤カウントする
   （2026-08-28 実測: 5分割の 68340 は C 27,600 → 5,200 だが AdjC は 5,520 → 5,200）。
3. 前日または当日の足が無い銘柄は `not_compared` に積み、**補間しない**。
4. スコープ（市場区分）を必ず持たせる。JPX 空売り集計は東証全体（ETF・REIT 込み）で
   対象範囲が違うため、騰落銘柄数と空売り代金を跨いだ除算は行わない。

## 公表値との差について（仕様として明記）

2026-08-28 のプライムで、本ルールの算出は 値上がり873 / 値下がり635 / 変わらず49。
日経公表値は 値上がり873 / 値下がり631 だった。値上がりは完全一致し、値下がりに
4銘柄（母集団 1,557 の 0.26%）の差が出る。原因は権利落ち銘柄などの集計慣行の違いで、
分割調整の誤りではない（AdjC 比較と「前日C×当日AdjFactor」補正は同一の判定になる）。
本アプリは公表値への追随ではなく、公式データからの決定論的な再現性を優先する。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from loguru import logger

from src.data_fetcher.jquants_api_client import (
    MARKET_CODE_GROWTH,
    MARKET_CODE_OTHER,
    MARKET_CODE_PRIME,
    MARKET_CODE_STANDARD,
    JQuantsApiClient,
)

BREADTH_SOURCE = "JQUANTS_V2"

# 市場区分コード → (スコープ名, 日本語表示名)。
# スコープ名は DB の breadth_scope 列にそのまま入れる識別子。
MARKET_SCOPES: dict[str, tuple[str, str]] = {
    MARKET_CODE_PRIME: ("TSE_PRIME", "プライム"),
    MARKET_CODE_STANDARD: ("TSE_STANDARD", "スタンダード"),
    MARKET_CODE_GROWTH: ("TSE_GROWTH", "グロース"),
    MARKET_CODE_OTHER: ("TSE_OTHER", "その他（ETF・REIT等）"),
}

# 需給モニターの既定スコープ。日経平均・TOPIX の主戦場であり、
# 空売り比率の解釈に使う「市場の広がり」としてはここが最も素直。
DEFAULT_BREADTH_SCOPE = "TSE_PRIME"


@dataclass(frozen=True)
class BreadthCounts:
    """1営業日・1市場区分ぶんの騰落銘柄数。"""

    date: str
    scope: str            # "TSE_PRIME" 等。異なるスコープ同士を混ぜないための識別子
    scope_label: str      # 画面表示用の日本語名
    advancing: int
    declining: int
    unchanged: int
    not_compared: int     # 前日/当日の足が無く判定できなかった銘柄数（補間しない）
    universe: int         # 対象日時点の母集団銘柄数
    source: str = BREADTH_SOURCE

    @property
    def compared(self) -> int:
        """実際に騰落を判定できた銘柄数。"""
        return self.advancing + self.declining + self.unchanged

    @property
    def net_breadth(self) -> Optional[float]:
        """(値上がり - 値下がり) / (値上がり + 値下がり)。-1.0〜+1.0。母数0なら None。"""
        base = self.advancing + self.declining
        if base == 0:
            return None
        return round((self.advancing - self.declining) / base, 4)

    @property
    def advance_decline_ratio(self) -> Optional[float]:
        """値上がり÷値下がり。値下がり0なら None（無限大を数値で誤魔化さない）。"""
        if self.declining == 0:
            return None
        return round(self.advancing / self.declining, 4)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["net_breadth"] = self.net_breadth
        data["advance_decline_ratio"] = self.advance_decline_ratio
        return data


@dataclass(frozen=True)
class IndexChange:
    """指数の1営業日ぶんの値動き。"""

    date: str
    name: str
    close: float
    prev_close: Optional[float]
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None

    @property
    def change(self) -> Optional[float]:
        if self.prev_close is None:
            return None
        return round(self.close - self.prev_close, 2)

    @property
    def change_pct(self) -> Optional[float]:
        if not self.prev_close:
            return None
        return round((self.close - self.prev_close) / self.prev_close * 100, 3)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["change"] = self.change
        data["change_pct"] = self.change_pct
        return data


# ----------------------------------------------------------------------
# 純粋関数（ネットワークに触れない。テストはここを直接叩く）
# ----------------------------------------------------------------------
def compute_breadth(
    target_date: str,
    bars_today: Iterable[dict],
    bars_prev: Iterable[dict],
    master: Iterable[dict],
    market_code: str,
) -> BreadthCounts:
    """1市場区分の騰落銘柄数を数える。

    Args:
        target_date: 対象営業日 "YYYY-MM-DD"
        bars_today:  対象日の全銘柄日足
        bars_prev:   前営業日の全銘柄日足
        master:      対象日時点の上場銘柄一覧
        market_code: 市場区分コード（"0111" 等）
    """
    scope, scope_label = MARKET_SCOPES.get(market_code, (f"TSE_{market_code}", market_code))

    today_by_code = {row["Code"]: row for row in bars_today if row.get("Code")}
    prev_by_code = {row["Code"]: row for row in bars_prev if row.get("Code")}
    universe = [row for row in master if row.get("Mkt") == market_code]

    advancing = declining = unchanged = not_compared = 0

    for issue in universe:
        code = issue.get("Code")
        today_row = today_by_code.get(code)
        prev_row = prev_by_code.get(code)
        if today_row is None or prev_row is None:
            not_compared += 1
            continue

        close = _adjusted_close(today_row)
        prev_close = _adjusted_close(prev_row)
        if close is None or prev_close is None:
            not_compared += 1
            continue

        if close > prev_close:
            advancing += 1
        elif close < prev_close:
            declining += 1
        else:
            unchanged += 1

    return BreadthCounts(
        date=target_date,
        scope=scope,
        scope_label=scope_label,
        advancing=advancing,
        declining=declining,
        unchanged=unchanged,
        not_compared=not_compared,
        universe=len(universe),
    )


def compute_all_breadth(
    target_date: str,
    bars_today: Iterable[dict],
    bars_prev: Iterable[dict],
    master: Iterable[dict],
    market_codes: Iterable[str] = tuple(MARKET_SCOPES),
) -> dict[str, BreadthCounts]:
    """市場区分ごとの騰落銘柄数を {scope: BreadthCounts} で返す。"""
    bars_today = list(bars_today)
    bars_prev = list(bars_prev)
    master = list(master)

    result: dict[str, BreadthCounts] = {}
    for market_code in market_codes:
        counts = compute_breadth(target_date, bars_today, bars_prev, master, market_code)
        result[counts.scope] = counts
    return result


def compute_topix_change(bars: Iterable[dict], target_date: str) -> Optional[IndexChange]:
    """TOPIX 四本値の時系列から対象日の騰落を組み立てる。

    前営業日はカレンダーではなく **返ってきた時系列の直前の足**で決める。
    休場日をまたいでも自然に前営業日になり、日付の引き算による1日ずれを避けられる。
    対象日の足が無ければ None（推測で埋めない）。
    """
    rows = sorted(
        (row for row in bars if row.get("Date") and row.get("C") is not None),
        key=lambda row: row["Date"],
    )
    for index, row in enumerate(rows):
        if row["Date"] != target_date:
            continue
        prev_close = rows[index - 1]["C"] if index > 0 else None
        return IndexChange(
            date=target_date,
            name="TOPIX",
            close=float(row["C"]),
            prev_close=float(prev_close) if prev_close is not None else None,
            open=_as_float(row.get("O")),
            high=_as_float(row.get("H")),
            low=_as_float(row.get("L")),
        )

    logger.warning(f"TOPIX の足が見つかりません: {target_date}")
    return None


def previous_business_day(calendar_rows: Iterable[dict], target_date: str) -> Optional[str]:
    """取引カレンダーから対象日の前営業日を返す（HolDiv=="1" が営業日）。

    日付を1日ずつ遡る従来の方法と違い、休場日の定義を公式データに委ねられる。
    """
    business_days = sorted(
        row["Date"]
        for row in calendar_rows
        if row.get("Date") and str(row.get("HolDiv")) == "1"
    )
    previous = [day for day in business_days if day < target_date]
    return previous[-1] if previous else None


# ----------------------------------------------------------------------
# IO（ネットワークに触れる。呼び出し側は fail-soft に扱うこと）
# ----------------------------------------------------------------------
def fetch_breadth_snapshot(
    target_date: str,
    prev_date: str,
    client: Optional[JQuantsApiClient] = None,
) -> dict[str, BreadthCounts]:
    """対象日の騰落銘柄数を J-Quants から取得して算出する。

    リクエストは3回（当日日足・前日日足・当日時点の銘柄一覧）。
    例外はそのまま送出するので、パイプライン側で握って fail-soft にすること。
    """
    client = client or JQuantsApiClient()
    bars_today = client.get_daily_bars(target_date)
    bars_prev = client.get_daily_bars(prev_date)
    master = client.get_listed_master(target_date)
    return compute_all_breadth(target_date, bars_today, bars_prev, master)


def fetch_topix_change(
    target_date: str,
    lookback_days: int = 10,
    client: Optional[JQuantsApiClient] = None,
) -> Optional[IndexChange]:
    """対象日の TOPIX 騰落を取得する。前営業日を含めるため少し前から取る。"""
    from datetime import datetime, timedelta

    client = client or JQuantsApiClient()
    end = datetime.strptime(target_date, "%Y-%m-%d")
    start = end - timedelta(days=lookback_days)
    bars = client.get_topix_bars(start.strftime("%Y-%m-%d"), target_date)
    return compute_topix_change(bars, target_date)


# ----------------------------------------------------------------------
def _adjusted_close(row: dict) -> Optional[float]:
    """調整後終値を返す。無ければ生の終値にフォールバックする。"""
    value = row.get("AdjC")
    if value is None:
        value = row.get("C")
    return _as_float(value)


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
