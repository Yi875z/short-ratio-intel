"""
売り圧力の判定に使う指標を組み立てる層。

## 守っている原則（依頼の算出ロジック要件をコードの構造で担保する）

1. **空売り比率は残高ではなく日次フロー**。
   JPX の空売り集計は「その日の売り注文代金の内訳」であって、未決済で残っている
   空売りの残高ではない。米国側の UsShortInterest（残高・隔週）とは別概念なので、
   日数（days to cover）や積み上がりの語彙をここへ持ち込まない。

2. **比率と絶対額を必ず分離する**。
   RatioBlock（%）と ValueBlock（百万円）を別の型にしてある。比率が同じでも
   商いが半分なら売り圧力の実額は半分であり、両者は別の情報である。
   片方だけを見て判定しないよう、レジーム層へは常に両方を渡す。

3. **異なる分母・対象市場を混ぜない**。
   比率の分母は必ず同一ソース内の合計売買代金(d)。JPX の公式定義に一致する
   （PDF本文に (a)/(d)・(b)/(d)・(c)/(d) と明記。2026-08-28 実物で確認）。
   騰落銘柄数は対象市場が違う（プライム等 vs 東証全体＋外国株券等）ため、
   BreadthBlock に scope を持たせたうえで、空売り代金と跨いだ除算は行わない。

4. **価格規制なしを弱気と断定しない**。
   規制なしは裁定・ヘッジ由来のことがある。without_share_pct（総空売りに占める
   構成比）を独立した値として持ち、レジーム層では「方向性売り判定の確信度を
   下げる材料」としてのみ使う。

5. **欠損を補間しない**。
   前日比・平均比・Zスコアはサンプルが足りなければ None を返す。
   前日値のコピーやゼロ埋めはしない。

## 単位

DB の *_va 列は **百万円**（JPX PDF の【単位：百万円】で確認済み）。
画面表示用の兆円換算は to_trillion_yen() を通す。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

import pandas as pd
from loguru import logger

from config.pressure_thresholds import PRESSURE_WINDOWS, PressureWindows

# DB の *_va 列の単位（百万円）を兆円へ直す係数
_MILLION_YEN_PER_TRILLION = 1_000_000


@dataclass(frozen=True)
class RatioBlock:
    """比率（%）。分母はすべて同一日・同一ソースの合計売買代金(d)。"""

    total_short_pct: Optional[float] = None          # (b+c)/d 空売り比率
    with_restriction_pct: Optional[float] = None     # (b)/d   価格規制あり比率
    without_restriction_pct: Optional[float] = None  # (c)/d   価格規制なし比率
    actual_order_pct: Optional[float] = None         # (a)/d   実注文比率
    without_share_pct: Optional[float] = None        # (c)/(b+c) 規制なし構成比

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ValueBlock:
    """絶対額（百万円）。比率とは別の情報として必ず併せて見る。"""

    total_short_va: Optional[float] = None
    with_restriction_va: Optional[float] = None
    without_restriction_va: Optional[float] = None
    actual_order_va: Optional[float] = None
    market_volume_va: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChangeBlock:
    """ある系列の変化を、水準ではなく相対で表したもの。"""

    label: str
    latest: Optional[float] = None
    dod_pct: Optional[float] = None        # 前営業日比（%）
    vs_avg_pct: Optional[float] = None     # 直近N営業日平均比（%）。当日は含めない
    zscore: Optional[float] = None         # 直近N営業日分布に対するZスコア
    sample_size: int = 0                   # Zスコアに使えたサンプル数

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BreadthBlock:
    """市場の広がり。scope が違うものを混ぜないため対象市場を明示して持つ。"""

    scope: Optional[str] = None
    scope_label: Optional[str] = None
    advancing: Optional[int] = None
    declining: Optional[int] = None
    unchanged: Optional[int] = None
    net_breadth: Optional[float] = None    # (上-下)/(上+下)
    available: bool = False                # 取得できたか（欠損時に判定へ使わせない）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PriceBlock:
    """価格反応。TOPIX の当日騰落率。"""

    topix_close: Optional[float] = None
    topix_prev_close: Optional[float] = None
    topix_change_pct: Optional[float] = None
    available: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PressureMetrics:
    """1営業日ぶんの需給指標一式。レジーム層はこれだけを受け取る。"""

    date: str
    ratios: RatioBlock = field(default_factory=RatioBlock)
    values: ValueBlock = field(default_factory=ValueBlock)
    short_value_change: ChangeBlock = field(
        default_factory=lambda: ChangeBlock(label="総空売り代金")
    )
    market_volume_change: ChangeBlock = field(
        default_factory=lambda: ChangeBlock(label="市場売買代金")
    )
    total_ratio_change: ChangeBlock = field(
        default_factory=lambda: ChangeBlock(label="空売り比率")
    )
    with_ratio_change: ChangeBlock = field(
        default_factory=lambda: ChangeBlock(label="価格規制あり比率")
    )
    price: PriceBlock = field(default_factory=PriceBlock)
    breadth: BreadthBlock = field(default_factory=BreadthBlock)
    missing_inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "ratios": self.ratios.to_dict(),
            "values": self.values.to_dict(),
            "short_value_change": self.short_value_change.to_dict(),
            "market_volume_change": self.market_volume_change.to_dict(),
            "total_ratio_change": self.total_ratio_change.to_dict(),
            "with_ratio_change": self.with_ratio_change.to_dict(),
            "price": self.price.to_dict(),
            "breadth": self.breadth.to_dict(),
            "missing_inputs": list(self.missing_inputs),
        }


# ----------------------------------------------------------------------
# 組み立て
# ----------------------------------------------------------------------
def build_pressure_metrics(
    target_date: str,
    market_df: pd.DataFrame,
    breadth_row: Optional[dict] = None,
    windows: PressureWindows = PRESSURE_WINDOWS,
) -> PressureMetrics:
    """需給指標を組み立てる。

    Args:
        target_date: 対象営業日 "YYYY-MM-DD"
        market_df:   market_short_ratio_daily の DataFrame（対象日を含む時系列）。
                     日付昇順に並べ替えたうえで、直前の行を前営業日として扱う。
        breadth_row: market_breadth_daily の1行ぶん（dict）。無ければブレッドスは
                     未取得として扱い、レジーム層はそれを前提に判定を落とす。
    """
    missing: list[str] = []

    if market_df is None or market_df.empty:
        logger.warning(f"{target_date}: 空売り集計データが無いため指標を組み立てられません")
        return PressureMetrics(date=target_date, missing_inputs=("空売り集計",))

    history = market_df.sort_values("date").reset_index(drop=True)
    history = history[history["date"] <= target_date]
    if history.empty or history.iloc[-1]["date"] != target_date:
        logger.warning(f"{target_date}: 対象日の空売り集計が見つかりません")
        return PressureMetrics(date=target_date, missing_inputs=("空売り集計",))

    today = history.iloc[-1]

    values = _build_values(today)
    ratios = _build_ratios(values, _as_float(today.get("short_ratio_pct")))
    # 分母の合計売買代金が無い日。保存済みの比率だけは採用できるため
    # ratios.total_short_pct は埋まるが、代金を必要とする判定は落とす必要がある。
    if not values.market_volume_va:
        missing.append("売買代金")
    if values.total_short_va is None:
        # 比率は分かるが内訳・絶対額が無い日。判定側がそれを前提に落とせるよう印を残す。
        missing.append("JPX内訳（空売り代金）")

    short_series = _series(history, "total_short_va", breakdown_only=True)
    volume_series = _series(history, "total_volume_va")
    # 空売り比率は保存済みの short_ratio_pct を正とする。内訳が欠けている日でも
    # 取得元から比率だけは得られており、内訳から再計算すると 0% になってしまう。
    ratio_series = _series(history, "short_ratio_pct")
    with_ratio_series = _ratio_series(history, "shrt_with_res_va")

    metrics = PressureMetrics(
        date=target_date,
        ratios=ratios,
        values=values,
        short_value_change=_build_change("総空売り代金", short_series, windows),
        market_volume_change=_build_change("市場売買代金", volume_series, windows),
        total_ratio_change=_build_change("空売り比率", ratio_series, windows),
        with_ratio_change=_build_change("価格規制あり比率", with_ratio_series, windows),
        price=_build_price(breadth_row),
        breadth=_build_breadth(breadth_row),
        missing_inputs=(),
    )

    if not metrics.price.available:
        missing.append("TOPIX騰落率")
    if not metrics.breadth.available:
        missing.append("騰落銘柄数")

    return _replace_missing(metrics, tuple(missing))


def _build_values(row) -> ValueBlock:
    """絶対額を取り出す。

    ⚠️ 内訳（実注文・規制あり・なし）が欠けている日は 0 ではなく None にする。
    stock-marketdata のスクレイパーは比率と売買代金しか持たず内訳を 0 で返すため、
    0 のまま扱うと「空売り代金が0円だった」という誤った事実になり、
    比率も 0% と表示され、レジーム判定まで誤る。
    """
    breakdown_missing = not _has_breakdown(row)
    return ValueBlock(
        total_short_va=None if breakdown_missing else _as_float(row.get("total_short_va")),
        with_restriction_va=None if breakdown_missing else _as_float(row.get("shrt_with_res_va")),
        without_restriction_va=None if breakdown_missing else _as_float(row.get("shrt_no_res_va")),
        actual_order_va=None if breakdown_missing else _as_float(row.get("sell_ex_short_va")),
        market_volume_va=_as_float(row.get("total_volume_va")),
    )


def _has_breakdown(row) -> bool:
    """JPX内訳が実際に入っているか（すべて0なら未取得とみなす）。"""
    return any(
        _as_float(row.get(column))
        for column in ("total_short_va", "shrt_with_res_va", "shrt_no_res_va")
    )


def _build_ratios(values: ValueBlock, stored_ratio_pct: Optional[float] = None) -> RatioBlock:
    """比率は必ず同一日の合計売買代金(d)を分母にする（JPXの公式定義と同じ）。

    内訳が欠けている日でも、空売り比率そのものは取得元から得られている。
    その場合は保存済みの short_ratio_pct を使い、内訳由来の比率だけ None にする。
    「内訳が無い」と「空売りが無かった」は別の事実である。
    """
    denominator = values.market_volume_va
    if not denominator:
        return RatioBlock(total_short_pct=stored_ratio_pct)

    total_short = values.total_short_va
    if total_short is None:
        # 内訳なし。比率は取得元の値をそのまま採用し、内訳の比率は出さない。
        return RatioBlock(total_short_pct=stored_ratio_pct)

    return RatioBlock(
        total_short_pct=_pct(total_short, denominator),
        with_restriction_pct=_pct(values.with_restriction_va, denominator),
        without_restriction_pct=_pct(values.without_restriction_va, denominator),
        actual_order_pct=_pct(values.actual_order_va, denominator),
        # 規制なし構成比だけは分母が総空売り。弱気/ヘッジの切り分け材料であって、
        # 市場全体に対する比率ではない点に注意。
        without_share_pct=_pct(values.without_restriction_va, total_short),
    )


def _build_change(
    label: str,
    series: Sequence[Optional[float]],
    windows: PressureWindows,
) -> ChangeBlock:
    """系列の末尾を当日として、前日比・平均比・Zスコアを出す。

    平均比とZスコアの窓には**当日を含めない**（当日を含めると自分自身で
    平均を押し上げ、極端な日ほど乖離が小さく見える）。

    ⚠️ 前日比は「1つ前の要素」とだけ比べる。欠測を詰めた列で比べると、
    8/26〜8/31 が欠測の場合に 9/1 を 8/25 と比較した結果を「前日比」として
    出してしまう（実際にそう表示していた）。前営業日が欠測なら None を返す。
    平均比とZスコアは欠測を詰めた窓で構わない（サンプル数を併記しているため）。
    """
    if not series:
        return ChangeBlock(label=label)

    # 当日が欠測なら、過去の値を「当日の値」として出さない。
    latest = series[-1]
    if latest is None:
        return ChangeBlock(label=label)

    previous = series[-2] if len(series) >= 2 else None
    prior = [v for v in series[:-1] if v is not None]

    dod_pct = None
    if previous:
        dod_pct = round((latest - previous) / previous * 100, 2)

    vs_avg_pct = None
    avg_window = prior[-windows.average_window:]
    if len(avg_window) >= windows.min_samples(windows.average_window):
        average = sum(avg_window) / len(avg_window)
        if average:
            vs_avg_pct = round((latest - average) / average * 100, 2)

    zscore = None
    z_window = prior[-windows.zscore_window:]
    sample_size = len(z_window)
    if sample_size >= windows.min_samples(windows.zscore_window):
        series_obj = pd.Series(z_window, dtype="float64")
        std = series_obj.std()
        if std and std > 0:
            zscore = round((latest - series_obj.mean()) / std, 2)

    return ChangeBlock(
        label=label,
        latest=latest,
        dod_pct=dod_pct,
        vs_avg_pct=vs_avg_pct,
        zscore=zscore,
        sample_size=sample_size,
    )


def _build_price(breadth_row: Optional[dict]) -> PriceBlock:
    if not breadth_row:
        return PriceBlock()
    change_pct = _as_float(breadth_row.get("topix_change_pct"))
    return PriceBlock(
        topix_close=_as_float(breadth_row.get("topix_close")),
        topix_prev_close=_as_float(breadth_row.get("topix_prev_close")),
        topix_change_pct=change_pct,
        available=change_pct is not None,
    )


def _build_breadth(breadth_row: Optional[dict]) -> BreadthBlock:
    if not breadth_row:
        return BreadthBlock()

    advancing = _as_int(breadth_row.get("advancing_issues"))
    declining = _as_int(breadth_row.get("declining_issues"))
    net = None
    if advancing is not None and declining is not None and (advancing + declining) > 0:
        net = round((advancing - declining) / (advancing + declining), 4)

    return BreadthBlock(
        scope=breadth_row.get("market_scope"),
        scope_label=breadth_row.get("scope_label"),
        advancing=advancing,
        declining=declining,
        unchanged=_as_int(breadth_row.get("unchanged_issues")),
        net_breadth=net,
        available=net is not None,
    )


# ----------------------------------------------------------------------
# 表示ヘルパー
# ----------------------------------------------------------------------
def to_trillion_yen(value: Optional[float]) -> Optional[float]:
    """百万円を兆円へ換算する（DBの *_va 列は百万円）。"""
    if value is None:
        return None
    return round(value / _MILLION_YEN_PER_TRILLION, 3)


def format_trillion_yen(value: Optional[float]) -> str:
    converted = to_trillion_yen(value)
    return "—" if converted is None else f"{converted:.2f}兆円"


def format_pct(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def format_signed_pct(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:+.{digits}f}%"


# ----------------------------------------------------------------------
def _series(history: pd.DataFrame, column: str, breakdown_only: bool = False) -> list[Optional[float]]:
    """列の時系列。

    breakdown_only=True の列は、その行の内訳が欠けていれば None にする。
    0 のまま平均やZスコアに入れると「空売りが激減した日」に見えてしまう。
    """
    if column not in history.columns:
        return []
    values = []
    for _, row in history.iterrows():
        if breakdown_only and not _has_breakdown(row):
            values.append(None)
        else:
            values.append(_as_float(row.get(column)))
    return values


def _ratio_series(history: pd.DataFrame, numerator_column: str) -> list[Optional[float]]:
    """比率の時系列。分母は必ず同じ行の合計売買代金にする。

    内訳が欠けている行は None（0%ではない）。
    """
    if numerator_column not in history.columns or "total_volume_va" not in history.columns:
        return []
    values = []
    for _, row in history.iterrows():
        if not _has_breakdown(row):
            values.append(None)
            continue
        values.append(_pct(_as_float(row.get(numerator_column)),
                           _as_float(row.get("total_volume_va"))))
    return values


def _replace_missing(metrics: PressureMetrics, missing: tuple[str, ...]) -> PressureMetrics:
    return PressureMetrics(
        date=metrics.date,
        ratios=metrics.ratios,
        values=metrics.values,
        short_value_change=metrics.short_value_change,
        market_volume_change=metrics.market_volume_change,
        total_ratio_change=metrics.total_ratio_change,
        with_ratio_change=metrics.with_ratio_change,
        price=metrics.price,
        breadth=metrics.breadth,
        missing_inputs=missing,
    )


def _pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


def _as_int(value) -> Optional[int]:
    result = _as_float(value)
    return None if result is None else int(result)
