"""
異常値検知モジュール
- 前日比急変（±3pt超）
- 自己比Zスコア逸脱（その業種の直近60営業日・当日除外から±2σ超）

2026-09-30: Zスコアは sector_insight.self_zscore に一本化した。それまで独自計算で、
当日を母集団に含め、渡された14日分（実際は約19営業日）で計算していた。同じプロンプトの中で
業種行の自己比Zと食い違い（例: 保険業 -2.64 と -2.18）、「過去最低水準」とまで書いていた。
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import (
    ANOMALY_DOD_THRESHOLD,
    ANOMALY_ZSCORE_THRESHOLD,
)
from src.analyzer.sector_insight import self_zscore


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
            history_df:    業種履歴（sector_insight.SECTOR_HISTORY_DAYS＝90暦日を渡す）
        """
        events: list[AnomalyEvent] = []
        sector_data = today_summary.get("sector_data", [])
        target_date = today_summary.get("date")

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
            z, _, samples = self_zscore(
                history_df, s["s33_code"], s["short_ratio_pct"], target_date
            )
            if z is not None and abs(z) >= ANOMALY_ZSCORE_THRESHOLD:
                severity = "high" if abs(z) >= 3.0 else "medium"
                # 「過去最高／最低」は言い過ぎ（Zは平均からの距離であって順位ではない）
                direction = "自己比で高水準" if z > 0 else "自己比で低水準"
                events.append(AnomalyEvent(
                    event_type="zscore_outlier",
                    sector_name=s["sector_name"],
                    s33_code=s["s33_code"],
                    current_ratio=s["short_ratio_pct"],
                    value=round(z, 2),
                    severity=severity,
                    description=f"自己比Z{z:+.2f}（{direction}・直近{samples}営業日比）",
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
