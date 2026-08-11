"""
バスケット集計と ETF 乖離（US-P2 の中核指標）

「指数のショートボリューム」は存在しない。そこで2系列を作り、その**乖離**を見る。

    構成銘柄加重比率 = Σ(member.short_volume) / Σ(member.reported_total_volume) * 100
    ETF比率          = SMH / SOXX 自身の FINRA ショート比率
    divergence       = z20(ETF比率) − z20(構成銘柄加重比率)

divergence が大きく正なら「ETF側だけショート増」＝機関のマクロ／セクターヘッジ。
大きく負なら「個別側だけショート増」＝テーマ内の銘柄選別。いずれも候補であって断定ではない。

⚠️ バスケット比率は必ずボリューム加重（比率の合算）で算出する。銘柄横断の単純平均は
   小型株の極端値に引きずられるため禁止（QCルール5）。
"""
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import US_ZSCORE_WINDOW_LONG, US_ZSCORE_WINDOW_SHORT
from config.us_universe import BASKET_PAIRS, BASKETS, basket_members
from src.analyzer.us_flow_analyzer import percentile_rank, zscore

# ETF側だけ／個別側だけにショートが偏っていると見なす乖離の目安
DIVERGENCE_THRESHOLD = 1.5


def build_basket_ratio_series(
    short_df: pd.DataFrame,
    members: list[str],
) -> pd.DataFrame:
    """構成銘柄をボリューム加重で合算した日次比率の時系列を返す。

    Returns:
        date / ratio / members_present / short_volume / reported_total_volume の DataFrame。
        分母が0または欠損の日は行を作らない（補間しない）。
    """
    if short_df is None or short_df.empty or not members:
        return pd.DataFrame()

    df = short_df[short_df["ticker"].isin(members)]
    if df.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for date_value, group in df.groupby("date", sort=True):
        # 単純平均ではなく、分子と分母をそれぞれ合計してから割る
        short_sum = group["short_volume"].sum(min_count=1)
        total_sum = group["reported_total_volume"].sum(min_count=1)
        if (
            short_sum is None or total_sum is None
            or pd.isna(short_sum) or pd.isna(total_sum) or total_sum <= 0
        ):
            continue

        ratio = float(short_sum) / float(total_sum) * 100
        if not (0.0 <= ratio <= 100.0):
            logger.warning(f"バスケット比率が範囲外のため除外: date={date_value} ratio={ratio}")
            continue

        rows.append({
            "date": date_value,
            "ratio": round(ratio, 4),
            "members_present": int(group["ticker"].nunique()),
            "short_volume": float(short_sum),
            "reported_total_volume": float(total_sum),
        })

    return pd.DataFrame(rows)


def add_series_zscores(
    series_df: pd.DataFrame,
    value_column: str = "ratio",
    window_short: int = US_ZSCORE_WINDOW_SHORT,
    window_long: int = US_ZSCORE_WINDOW_LONG,
) -> pd.DataFrame:
    """時系列に z20 / z60 / pct60 を付ける（当日を履歴に含めない）。"""
    if series_df is None or series_df.empty:
        return pd.DataFrame()

    df = series_df.sort_values("date").reset_index(drop=True)
    values = df[value_column].tolist()

    def _or_nan(value: Optional[float]) -> float:
        # 0.0 は正当な値なので truthiness で潰さないこと
        return np.nan if value is None else value

    z_short: list[float] = []
    z_long: list[float] = []
    pct_long: list[float] = []
    for i in range(len(df)):
        history = values[:i]
        current = values[i]
        z_short.append(_or_nan(zscore(history, current, window_short)))
        z_long.append(_or_nan(zscore(history, current, window_long)))
        pct_long.append(_or_nan(percentile_rank(history, current, window_long)))

    df["z20"] = z_short
    df["z60"] = z_long
    df["pct60"] = pct_long
    return df


def build_basket_metrics(
    short_df: pd.DataFrame,
    basket_name: str,
) -> pd.DataFrame:
    """バスケット名を指定して比率＋Zスコアの時系列を返す。"""
    members = basket_members(basket_name)
    if not members:
        logger.warning(f"未定義のバスケット: {basket_name}")
        return pd.DataFrame()

    series = build_basket_ratio_series(short_df, members)
    if series.empty:
        return series

    metrics = add_series_zscores(series)
    metrics["basket"] = basket_name
    metrics["members_expected"] = len(members)
    return metrics


def build_ticker_series(short_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """単一ティッカー（ETF等）の比率時系列＋Zスコアを返す。"""
    if short_df is None or short_df.empty:
        return pd.DataFrame()

    df = short_df[short_df["ticker"] == ticker][["date", "short_ratio_pct"]]
    if df.empty:
        return pd.DataFrame()

    series = df.rename(columns={"short_ratio_pct": "ratio"}).dropna(subset=["ratio"])
    if series.empty:
        return pd.DataFrame()

    metrics = add_series_zscores(series)
    metrics["ticker"] = ticker
    return metrics


def _latest_value(df: pd.DataFrame, column: str, date_value: str) -> Optional[float]:
    if df is None or df.empty or column not in df.columns:
        return None
    matched = df[df["date"] == date_value]
    if matched.empty:
        return None
    value = matched.iloc[0][column]
    if value is None or pd.isna(value):
        return None
    return float(value)


def compute_divergence(
    short_df: pd.DataFrame,
    basket_name: str,
    etf_ticker: str,
    target_date: str,
) -> dict:
    """指定日の ETF と構成銘柄加重の乖離を返す。

    Returns:
        basket / etf / それぞれの ratio と z20 / divergence / interpretation を含む dict。
        判定に必要な値が欠ける場合 divergence は None（推測で埋めない）。
    """
    basket_metrics = build_basket_metrics(short_df, basket_name)
    etf_metrics = build_ticker_series(short_df, etf_ticker)

    basket_z = _latest_value(basket_metrics, "z20", target_date)
    etf_z = _latest_value(etf_metrics, "z20", target_date)

    divergence = None
    if basket_z is not None and etf_z is not None:
        divergence = round(etf_z - basket_z, 4)

    return {
        "date": target_date,
        "basket": basket_name,
        "basket_ratio": _latest_value(basket_metrics, "ratio", target_date),
        "basket_z20": basket_z,
        "etf": etf_ticker,
        "etf_ratio": _latest_value(etf_metrics, "ratio", target_date),
        "etf_z20": etf_z,
        "divergence": divergence,
        "interpretation": interpret_divergence(divergence),
    }


def interpret_divergence(divergence: Optional[float]) -> str:
    """乖離の読み方を返す。いずれも候補であり断定ではない。"""
    if divergence is None:
        return "判定不能（データ不足）"
    if divergence > DIVERGENCE_THRESHOLD:
        return "ETF側のみショート増。マクロ／セクターヘッジ候補（個別ファンダは無傷の可能性）"
    if divergence < -DIVERGENCE_THRESHOLD:
        return "個別側のみショート増。テーマ内の銘柄選別が進行している候補"
    return "ETFと構成銘柄が連動。セクター全体が同方向"


def compute_basket_spread(
    short_df: pd.DataFrame,
    long_basket: str,
    short_basket: str,
    target_date: str,
) -> dict:
    """ロング候補 / ショート候補の2バスケット間のZスコア差を返す。

    spread = z20(ショート側) − z20(ロング側)。
    プラスが大きいほど、ショート側に売りが偏っている＝その対の取引が入っている候補。

    ⚠️ 比率そのものの引き算ではない。水準は銘柄群ごとに違う（SaaSは常時60%台、
       半導体は40%台）ため、それぞれの過去分布からの乖離度（Zスコア）で比べる。
    """
    long_metrics = build_basket_metrics(short_df, long_basket)
    short_metrics = build_basket_metrics(short_df, short_basket)

    long_z = _latest_value(long_metrics, "z20", target_date)
    short_z = _latest_value(short_metrics, "z20", target_date)

    spread = None
    if long_z is not None and short_z is not None:
        spread = round(short_z - long_z, 4)

    return {
        "date": target_date,
        "long_basket": long_basket,
        "long_ratio": _latest_value(long_metrics, "ratio", target_date),
        "long_z20": long_z,
        "short_basket": short_basket,
        "short_ratio": _latest_value(short_metrics, "ratio", target_date),
        "short_z20": short_z,
        "spread": spread,
        "interpretation": interpret_spread(spread, long_basket, short_basket),
    }


def interpret_spread(
    spread: Optional[float],
    long_basket: str,
    short_basket: str,
) -> str:
    """ペアの偏りの読み方を返す。いずれも候補であり断定ではない。"""
    if spread is None:
        return "判定不能（データ不足）"
    if spread > DIVERGENCE_THRESHOLD:
        return f"{short_basket}側に売りが偏っている。この対の取引が入っている候補"
    if spread < -DIVERGENCE_THRESHOLD:
        return f"{long_basket}側に売りが偏っている。想定と逆向きの偏り"
    return "どちらにも偏っていない。ペアとしての動きは出ていない"


def build_all_basket_spreads(short_df: pd.DataFrame, target_date: str) -> list[dict]:
    """設定済みの全ペアについてスプレッドを返す（レポート用）。"""
    results: list[dict] = []
    for pair in BASKET_PAIRS:
        record = compute_basket_spread(
            short_df, pair["long"], pair["short"], target_date
        )
        record["name"] = pair["name"]
        record["note"] = pair["note"]
        results.append(record)
    return results


def build_all_basket_metrics(short_df: pd.DataFrame, target_date: str) -> list[dict]:
    """全バスケットの最新指標をまとめて返す（レポート用）。"""
    results: list[dict] = []
    for basket_name in BASKETS:
        metrics = build_basket_metrics(short_df, basket_name)
        if metrics.empty:
            continue
        row = metrics[metrics["date"] == target_date]
        if row.empty:
            continue
        record = row.iloc[0]

        # 前営業日比（pt）。直前行が無ければ None
        previous = metrics[metrics["date"] < target_date].tail(1)
        dod = None
        if not previous.empty:
            dod = round(float(record["ratio"]) - float(previous.iloc[0]["ratio"]), 4)

        results.append({
            "basket": basket_name,
            "ratio": float(record["ratio"]),
            "z20": None if pd.isna(record["z20"]) else float(record["z20"]),
            "z60": None if pd.isna(record["z60"]) else float(record["z60"]),
            "pct60": None if pd.isna(record["pct60"]) else float(record["pct60"]),
            "dod_change": dod,
            "members_present": int(record["members_present"]),
            "members_expected": int(record["members_expected"]),
        })
    return results
