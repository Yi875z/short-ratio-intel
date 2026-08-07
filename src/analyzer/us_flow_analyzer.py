"""
米国ショートフローのローリング統計（US-P1）

日次ショート比率を「銘柄自身の過去分布に対する相対位置」で評価する。
絶対閾値（50%超え＝弱気）による判定は行わない（QCルール3）。

判定はすべて候補（candidate）扱いであり、単日データで断定しない（QCルール4）。
4象限分類・バスケット集計は US-P2 で本モジュールの上に載せる。
"""
from typing import Optional, Sequence

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import (
    US_MIN_SAMPLE_COVERAGE,
    US_ZSCORE_WINDOW_LONG,
    US_ZSCORE_WINDOW_SHORT,
)


def _clean(values: Sequence) -> list[float]:
    """None / NaN を除いた float のリストを返す（欠損は補間しない）。"""
    cleaned: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if np.isnan(f):
            continue
        cleaned.append(f)
    return cleaned


def zscore(
    history: Sequence,
    current: Optional[float],
    window: int,
    min_coverage: float = US_MIN_SAMPLE_COVERAGE,
) -> Optional[float]:
    """直近 window 件の履歴に対する current の Zスコアを返す。

    Args:
        history: 当日を含まない過去の値（古い順）
        current: 当日の値
        window:  窓幅（営業日数）
        min_coverage: 窓幅に対して必要な最低サンプル比率

    Returns:
        Zスコア。以下の場合は判定せず None を返す。
        - current が欠損
        - 有効サンプル数が window * min_coverage 未満
        - 標準偏差が 0（分母0で例外を出さない）
    """
    if current is None:
        return None
    try:
        current_value = float(current)
    except (TypeError, ValueError):
        return None
    if np.isnan(current_value):
        return None

    samples = _clean(history)[-window:]
    if len(samples) < window * min_coverage:
        return None

    mean = float(np.mean(samples))
    # 母集団標準偏差（ddof=0）。サンプル数が窓幅で固定のため一貫性を優先する
    std = float(np.std(samples))
    if std == 0.0 or np.isnan(std):
        return None

    return round((current_value - mean) / std, 4)


def percentile_rank(
    history: Sequence,
    current: Optional[float],
    window: int,
    min_coverage: float = US_MIN_SAMPLE_COVERAGE,
) -> Optional[float]:
    """直近 window 件の分布における current のパーセンタイル(0〜100)を返す。"""
    if current is None:
        return None
    try:
        current_value = float(current)
    except (TypeError, ValueError):
        return None
    if np.isnan(current_value):
        return None

    samples = _clean(history)[-window:]
    if len(samples) < window * min_coverage:
        return None

    below = sum(1 for s in samples if s <= current_value)
    return round(below / len(samples) * 100, 2)


def close_location_value(
    high: Optional[float],
    low: Optional[float],
    close: Optional[float],
) -> Optional[float]:
    """終値位置 CLV を返す。−1（安値引け）〜 +1（高値引け）。

    高値と安値が同値（値幅ゼロ）の場合は判定不能として None。
    """
    if high is None or low is None or close is None:
        return None
    try:
        h, l, c = float(high), float(low), float(close)
    except (TypeError, ValueError):
        return None
    if np.isnan(h) or np.isnan(l) or np.isnan(c):
        return None

    span = h - l
    if span <= 0:
        return None
    return round(((c - l) - (h - c)) / span, 4)


def build_flow_metrics(
    short_df: pd.DataFrame,
    price_df: Optional[pd.DataFrame] = None,
    window_short: int = US_ZSCORE_WINDOW_SHORT,
    window_long: int = US_ZSCORE_WINDOW_LONG,
    tail_rows: Optional[int] = None,
) -> pd.DataFrame:
    """銘柄ごとのローリング統計を付加した DataFrame を返す。

    Args:
        short_df: db.get_us_short_volume_df() の戻り（date, ticker, short_ratio_pct ほか）
        price_df: db.get_us_market_daily_df() の戻り（任意。あれば騰落率・CLV・出来高比を付ける）
        tail_rows: 指定すると銘柄ごとの直近N営業日ぶんだけを返す。
                   Zスコアの窓には全履歴を使うので値は変わらない。
                   日次レポートのように最新日しか要らない場合、全履歴ぶんの
                   計算を避けるために使う（249営業日で実測1.7秒→0.1秒）。

    Returns:
        入力に z20 / z60 / pct60 / daily_return / clv / volume_ratio を足した DataFrame。
        判定できない箇所は欠損のまま（前日値のコピーや補間は行わない）。
    """
    if short_df is None or short_df.empty:
        return pd.DataFrame()

    df = short_df.copy()
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    # 価格データを結合（無くても動く）
    if price_df is not None and not price_df.empty:
        price_cols = ["date", "ticker", "high", "low", "close", "market_volume"]
        available = [c for c in price_cols if c in price_df.columns]
        df = df.merge(
            price_df[available], on=["date", "ticker"], how="left", suffixes=("", "_px")
        )

    has_price = "close" in df.columns

    # 行インデックスを指定して書き戻す。groupby の反復順に依存しない
    metric_columns = ("z20", "z60", "pct60", "daily_return", "clv", "volume_ratio")
    metrics = {
        column: pd.Series(np.nan, index=df.index, dtype="float64")
        for column in metric_columns
    }

    keep_indexes: list = []

    for _, group in df.groupby("ticker", sort=False):
        row_indexes = list(group.index)
        # 計算対象を直近N営業日に絞る（履歴は下の ratios[:i] で全期間を参照するため値は不変）
        first_computed = 0 if tail_rows is None else max(0, len(row_indexes) - tail_rows)
        keep_indexes.extend(row_indexes[first_computed:])
        ratios = group["short_ratio_pct"].tolist()
        closes = group["close"].tolist() if has_price else [None] * len(group)
        highs = group["high"].tolist() if has_price else [None] * len(group)
        lows = group["low"].tolist() if has_price else [None] * len(group)
        volumes = (
            group["market_volume"].tolist()
            if has_price and "market_volume" in group.columns
            else [None] * len(group)
        )

        for i, row_index in enumerate(row_indexes):
            if i < first_computed:
                continue
            history = ratios[:i]          # 当日を含めない
            current = ratios[i]

            def _set(column: str, value: Optional[float]) -> None:
                if value is not None:
                    metrics[column].at[row_index] = value

            _set("z20", zscore(history, current, window_short))
            _set("z60", zscore(history, current, window_long))
            _set("pct60", percentile_rank(history, current, window_long))

            # 騰落率（前営業日終値比）
            prev_close = closes[i - 1] if i > 0 else None
            if prev_close is not None and closes[i] is not None:
                try:
                    prev_value = float(prev_close)
                    if prev_value and not np.isnan(prev_value):
                        _set("daily_return", round(float(closes[i]) / prev_value - 1.0, 6))
                except (TypeError, ValueError):
                    pass

            _set("clv", close_location_value(highs[i], lows[i], closes[i]))

            # 出来高比（対 直近20営業日平均）。分母は consolidated volume だが
            # ここは文脈把握用であり、ショート比率の分母には使わない
            vol_history = _clean(volumes[:i])[-window_short:]
            if volumes[i] is not None and len(vol_history) >= window_short * US_MIN_SAMPLE_COVERAGE:
                mean_vol = float(np.mean(vol_history))
                if mean_vol > 0:
                    try:
                        _set("volume_ratio", round(float(volumes[i]) / mean_vol, 4))
                    except (TypeError, ValueError):
                        pass

    for column in metric_columns:
        df[column] = metrics[column]

    if tail_rows is not None:
        # 計算していない行を「判定不能」と誤読させないよう、返す範囲自体を絞る
        df = df.loc[sorted(keep_indexes)].reset_index(drop=True)

    return df


def latest_flow_metrics(
    short_df: pd.DataFrame,
    price_df: Optional[pd.DataFrame] = None,
    target_date: Optional[str] = None,
) -> pd.DataFrame:
    """最新日（または指定日）のフロー指標だけを返す。"""
    metrics = build_flow_metrics(short_df, price_df)
    if metrics.empty:
        return metrics

    date_value = target_date or metrics["date"].max()
    result = metrics[metrics["date"] == date_value].reset_index(drop=True)
    if result.empty:
        logger.warning(f"米国フロー指標: {date_value} のデータがありません")
    return result
