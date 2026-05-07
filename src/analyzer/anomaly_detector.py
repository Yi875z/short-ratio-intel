"""
異常値検知モジュール
- 前日比急変（±3pt超）
- Zスコア逸脱（過去30日から±2σ超）
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import (
    ANOMALY_DOD_THRESHOLD,
    ANOMALY_ZSCORE_THRESHOLD,
    HISTORY_DAYS_FOR_ZSCORE,
)


@dataclass
class AnomalyEvent:
    event_type: str        # "dod_spike" | "zscore_outlier" | "absolute_extreme"
    sector_name: str
    s33_code: str
    current_ratio: float
    value: float           # 前日比 or Zスコア
    severity: str          # "high" | "medium"
    description: str


class AnomalyDetector:
    """空売り比率の異常値を検知する"""

    def detect(
        self,
        today_summary: dict,
        history_df: pd.DataFrame,
    ) -> list[AnomalyEvent]:
        """
        異常値を検知してリストで返す。

        Args:
            today_summary: RatioCalculator.get_today_summary() の結果
            history_df:    過去30日のデータ DataFrame
        """
        events: list[AnomalyEvent] = []
        sector_data = today_summary.get("sector_data", [])

        for s in sector_data:
            # ① 前日比急変
            dod = s.get("dod_change")
            if dod is not None and abs(dod) >= ANOMALY_DOD_THRESHOLD:
                severity = "high" if abs(dod) >= 5.0 else "medium"
                direction = "急騰" if dod > 0 else "急落"
                events.append(AnomalyEvent(
                    event_type="dod_spike",
                    sector_name=s["sector_name"],
                    s33_code=s["s33_code"],
                    current_ratio=s["short_ratio_pct"],
                    value=dod,
                    severity=severity,
                    description=f"前日比{direction}: {dod:+.1f}pt",
                ))

            # ② Zスコア逸脱
            z = self._calc_zscore(s["s33_code"], s["short_ratio_pct"], history_df)
            if z is not None and abs(z) >= ANOMALY_ZSCORE_THRESHOLD:
                severity = "high" if abs(z) >= 3.0 else "medium"
                direction = "過去最高水準" if z > 0 else "過去最低水準"
                events.append(AnomalyEvent(
                    event_type="zscore_outlier",
                    sector_name=s["sector_name"],
                    s33_code=s["s33_code"],
                    current_ratio=s["short_ratio_pct"],
                    value=z,
                    severity=severity,
                    description=f"Zスコア{z:+.2f}（{direction}）",
                ))

            # ③ 絶対値極端
            ratio = s["short_ratio_pct"]
            if ratio >= 55.0:
                events.append(AnomalyEvent(
                    event_type="absolute_extreme",
                    sector_name=s["sector_name"],
                    s33_code=s["s33_code"],
                    current_ratio=ratio,
                    value=ratio,
                    severity="high",
                    description=f"絶対値55%超の異常高水準: {ratio:.1f}%",
                ))

        logger.info(f"異常値検知: {len(events)}件")
        return events

    def _calc_zscore(
        self,
        s33_code: str,
        current_ratio: float,
        history_df: pd.DataFrame,
    ) -> Optional[float]:
        """過去30日のZスコアを計算"""
        if history_df.empty:
            return None

        sector_hist = history_df[history_df["s33_code"] == s33_code]["short_ratio_pct"]
        if len(sector_hist) < 5:  # データ不足
            return None

        mean = sector_hist.mean()
        std = sector_hist.std()
        if std == 0:
            return None

        return round((current_ratio - mean) / std, 2)
