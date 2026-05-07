"""
シグナル判定・ダッシュボード表示で使うしきい値。

相場環境に応じた調整をここへ集約し、判定ロジック内の固定値を避ける。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SignalThresholds:
    # 市場全体の水準判定
    market_normal_lower_pct: float = 37.0
    market_warning_pct: float = 43.0
    market_directional_min_pct: float = 40.0
    market_directional_with_min_pct: float = 30.0
    market_directional_with_high_pct: float = 35.0

    # ヘッジ・裁定混入判定
    no_restriction_share_warning_pct: float = 35.0
    no_restriction_ratio_warning_pct: float = 10.0

    # ショートカバー・売り枯れ判定
    market_cover_ratio_min_pct: float = 40.0
    sector_cover_ratio_min_pct: float = 43.0
    cover_dod_drop_pt: float = -3.0
    sector_exhaustion_ratio_max_pct: float = 32.0
    exhaustion_dod_drop_pt: float = -3.0

    # その他（33業種外）の市場影響
    other_volume_share_warning_pct: float = 8.0

    # 業種別の方向性売り判定
    sector_directional_min_pct: float = 47.0
    sector_directional_high_pct: float = 50.0
    sector_directional_with_min_pct: float = 35.0

    # 表示・レポート上の履歴解釈
    persistent_signal_days: int = 5

    # JPX内訳の簡易ラベル
    weak_directional_with_pct: float = 28.0


SIGNAL_THRESHOLDS = SignalThresholds()
