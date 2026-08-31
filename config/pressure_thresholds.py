"""
需給モニター（売り圧力レジーム）で使うしきい値と窓幅。

判定ロジック内に固定値を散らさないため、ここへ集約する。
既存の signal_thresholds.py と同じ dataclass パターンを踏襲している。

設計方針: 単一の固定閾値で断定しない。
    絶対水準（「空売り比率40%超」など）だけで判定すると、平常時の水準が違う局面で
    同じ意味に読めない。指標は原則として「自分自身の直近分布に対する相対位置」
    （Zスコア・平均比）へ変換してから閾値に掛ける。米国側（us_flow_classifier）と
    同じ考え方を日本側にも入れる。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PressureWindows:
    """相対評価に使う窓幅とサンプル要件。"""

    # 短期Zスコアの窓（営業日）。当日は含めず、直前N営業日の分布と比べる。
    zscore_window: int = 20
    # 平均比を取る窓（営業日）。当日は含めない。
    average_window: int = 5
    # 窓幅に対して必要な最低サンプル比率。下回れば判定せず None を返す。
    # 欠損を補間して無理に数値を出すと、根拠のない判定が下流へ流れる。
    min_sample_coverage: float = 0.8

    def min_samples(self, window: int) -> int:
        return max(2, int(round(window * self.min_sample_coverage)))


@dataclass(frozen=True)
class PressureThresholds:
    """レジーム判定の基準値。

    Zスコア系は「その指標が自分の直近20営業日に対して何σ離れているか」。
    平均比系は「直近5営業日平均に対して何%か」。
    """

    # --- 絶対額（空売り代金）の増加 ---
    short_value_surge_z: float = 1.0        # 明確な増加
    short_value_mild_z: float = 0.5         # 緩やかな増加
    short_value_quiet_z: float = 0.5        # これ未満なら「実額は増えていない」

    # --- 流動性（市場売買代金）---
    volume_thin_z: float = -1.0             # 商いが細い
    volume_thin_vs_avg_pct: float = -15.0   # 5日平均比でこれ以下なら細い
    volume_active_z: float = 0.0            # 商いが細くない

    # --- 空売り比率そのもの ---
    ratio_elevated_z: float = 0.5           # 比率が高め
    ratio_high_z: float = 1.0               # 比率が明確に高い
    with_restriction_elevated_z: float = 0.5  # 価格規制あり比率が高め

    # --- ショートカバー ---
    cover_ratio_drop_pt: float = -3.0       # 空売り比率の前日比（ptで見る）
    cover_short_value_vs_avg_pct: float = 0.0  # 空売り代金が5日平均を下回る

    # --- 価格反応 ---
    price_down_pct: float = 0.0             # TOPIX騰落率がこれ未満なら下落
    price_up_pct: float = 0.0               # これ超なら上昇

    # --- 市場の広がり（騰落銘柄数）---
    # net_breadth = (値上がり - 値下がり) / (値上がり + 値下がり)、-1.0〜+1.0
    breadth_broad_decline: float = -0.30    # 全面安と呼べる水準
    breadth_broad_advance: float = 0.30

    # --- 価格規制なしの扱い ---
    # 「規制なしが多い＝弱気」と断定しない。裁定・ヘッジ由来の可能性があるため、
    # この構成比を超えたら方向性売りの判定確信度を下げる（判定を消しはしない）。
    without_share_dilution_pct: float = 35.0


PRESSURE_WINDOWS = PressureWindows()
PRESSURE_THRESHOLDS = PressureThresholds()
