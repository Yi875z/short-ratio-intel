"""
米国ショートフローの4象限パターン分類（US-P2）

日次ショートボリュームは「フロー」であり残高ではない。当日中に買い戻された分・
マーケットメイクの一時的ショート・顧客の現物売却を仲介するショートを含む。
したがって本モジュールが返すタグは**すべて候補（candidate）**であり、
「機関が売っている」のような確定表現に使ってはならない（QCルール4）。

残高側（Short Interest）の裏付けが取れて初めて CONFIRMED へ昇格させる。
その昇格ロジックは US-P3 で実装する。
"""
from typing import Optional

import numpy as np
import pandas as pd

# --- タグ定義 ---
SELL_PRESSURE = "SELL_PRESSURE"          # 売り圧力強化候補
SHORT_ABSORBED = "SHORT_ABSORBED"        # 売り吸収・ショート逆行候補
LONG_LIQUIDATION = "LONG_LIQUIDATION"    # ロング清算・現物売り主導候補
SQUEEZE_BUILDING = "SQUEEZE_BUILDING"    # 買い戻し圧力の蓄積候補
NEUTRAL = "NEUTRAL"                      # 該当なし
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # 判定に必要な値が欠けている

PATTERN_LABELS: dict[str, str] = {
    SELL_PRESSURE: "売り圧力強化候補",
    SHORT_ABSORBED: "売り吸収・ショート逆行候補",
    LONG_LIQUIDATION: "ロング清算・現物売り主導候補",
    SQUEEZE_BUILDING: "買い戻し圧力の蓄積候補",
    NEUTRAL: "特段のシグナルなし",
    INSUFFICIENT_DATA: "判定不能（データ不足）",
}

# --- 閾値 ---
_Z_ELEVATED = 1.5        # ショート比率が明確に高い
_Z_MILD = 1.0            # やや高い（スクイーズ蓄積の下限）
_Z_DEPRESSED = -1.0      # ショート比率が明確に低い
_CLV_WEAK_CLOSE = -0.3   # 安値引け寄り
_CLV_STRONG_CLOSE = 0.3  # 高値引け寄り
_VOLUME_SURGE = 1.2      # 出来高が平常より多い
_FLAT_RETURN = 0.005     # 「ほぼ動かなかった」とみなす騰落率
_SQUEEZE_MIN_DAYS = 3    # 蓄積とみなす連続営業日数


def _value(row, key: str) -> Optional[float]:
    """DataFrame の行から数値を取り出す。欠損（NaN/None）は None に統一する。"""
    if key not in row:
        return None
    raw = row[key]
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if np.isnan(value):
        return None
    return value


def classify_row(row, flat_streak: int = 0) -> str:
    """1営業日ぶんのフロー指標からパターンタグを返す。

    Args:
        row: z20 / daily_return / clv / volume_ratio を持つ dict または Series
        flat_streak: 「z20が高く、かつほぼ動かなかった」状態が当日まで
                     何営業日continuousで続いているか（当日を含む）

    判定順序は SELL_PRESSURE → SHORT_ABSORBED → SQUEEZE_BUILDING → LONG_LIQUIDATION。
    条件が重なった場合は、より具体的（当日の値動きと出来高まで揃っている）ものを優先する。
    """
    z20 = _value(row, "z20")
    if z20 is None:
        # Zスコアが出せない＝過去分布が足りない。推測で埋めずに判定不能を返す
        return INSUFFICIENT_DATA

    daily_return = _value(row, "daily_return")
    clv = _value(row, "clv")
    volume_ratio = _value(row, "volume_ratio")

    if (
        z20 > _Z_ELEVATED
        and daily_return is not None and daily_return < 0
        and clv is not None and clv < _CLV_WEAK_CLOSE
        and volume_ratio is not None and volume_ratio > _VOLUME_SURGE
    ):
        return SELL_PRESSURE

    if (
        z20 > _Z_ELEVATED
        and daily_return is not None and daily_return > 0
        and clv is not None and clv > _CLV_STRONG_CLOSE
    ):
        return SHORT_ABSORBED

    if z20 > _Z_MILD and flat_streak >= _SQUEEZE_MIN_DAYS:
        return SQUEEZE_BUILDING

    if z20 < _Z_DEPRESSED and daily_return is not None and daily_return < 0:
        return LONG_LIQUIDATION

    return NEUTRAL


def _is_flat_and_elevated(row) -> bool:
    """「ショート比率が高いのに株価がほぼ動かない」日か。"""
    z20 = _value(row, "z20")
    daily_return = _value(row, "daily_return")
    if z20 is None or daily_return is None:
        return False
    return z20 > _Z_MILD and abs(daily_return) < _FLAT_RETURN


def classify_flow_metrics(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """build_flow_metrics() の出力に pattern 列を付けて返す。

    SQUEEZE_BUILDING は連続性を見るため、銘柄ごとに時系列順で走査する。
    """
    if metrics_df is None or metrics_df.empty:
        return pd.DataFrame()

    df = metrics_df.copy()
    if "date" in df.columns and "ticker" in df.columns:
        df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    patterns = pd.Series("", index=df.index, dtype="object")

    for _, group in df.groupby("ticker", sort=False):
        streak = 0
        for row_index in group.index:
            row = df.loc[row_index]
            streak = streak + 1 if _is_flat_and_elevated(row) else 0
            patterns.at[row_index] = classify_row(row, flat_streak=streak)

    df["pattern"] = patterns
    df["pattern_label"] = df["pattern"].map(PATTERN_LABELS).fillna("")
    return df


def summarize_patterns(classified_df: pd.DataFrame) -> dict[str, int]:
    """パターンごとの銘柄数を返す（判定不能も含めて数える）。"""
    if classified_df is None or classified_df.empty or "pattern" not in classified_df:
        return {}
    return classified_df["pattern"].value_counts().to_dict()
