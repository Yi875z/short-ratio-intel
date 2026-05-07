"""
JPX空売り内訳から投資判断用シグナルを生成する。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from config.signal_thresholds import SIGNAL_THRESHOLDS


@dataclass
class FlowSignal:
    category: str
    target: str
    signal: str
    severity: str
    rationale: str
    watch_point: str
    details: list[str]
    invalidation_condition: str

    def to_dict(self) -> dict:
        return asdict(self)


class FlowSignalAnalyzer:
    """総空売り・価格規制あり/なし・その他カテゴリからシグナルを判定する。"""

    def __init__(self, thresholds=SIGNAL_THRESHOLDS):
        self.thresholds = thresholds

    def detect(
        self,
        today_summary: dict,
        market_trend_df: pd.DataFrame,
    ) -> list[dict]:
        signals: list[FlowSignal] = []
        signals.extend(self._detect_market_signals(today_summary, market_trend_df))
        signals.extend(self._detect_sector_signals(today_summary))
        return [s.to_dict() for s in signals]

    def build_history(
        self,
        dates: list[str],
        summary_by_date: dict[str, dict],
        market_trend_df: pd.DataFrame,
    ) -> dict:
        """保存済み日付のシグナル履歴を集計する。"""
        if not dates:
            return {"rows": [], "timeline": pd.DataFrame(), "latest": []}

        ordered_dates = sorted(dates)
        rows = []
        signal_dates: dict[tuple[str, str, str], list[str]] = {}

        for current_date in ordered_dates:
            summary = summary_by_date.get(current_date, {})
            if not summary:
                continue

            current_market_df = pd.DataFrame()
            if not market_trend_df.empty:
                current_market_df = market_trend_df[market_trend_df["date"] <= current_date].copy()

            for signal in self.detect(summary, current_market_df):
                key = (signal["category"], signal["target"], signal["signal"])
                signal_dates.setdefault(key, []).append(current_date)

        latest_date = ordered_dates[-1]
        previous_date = ordered_dates[-2] if len(ordered_dates) >= 2 else None
        latest_rows = []

        for (category, target, signal_name), active_dates in signal_dates.items():
            active_set = set(active_dates)
            active_today = latest_date in active_set
            active_previous = previous_date in active_set if previous_date else False

            if active_today and active_previous:
                state = "継続"
            elif active_today:
                state = "新規"
            elif active_previous:
                state = "消滅"
            else:
                state = "過去"

            if state == "過去":
                continue

            streak = _calc_streak(ordered_dates, active_set, latest_date) if active_today else 0
            rows.append({
                "category": category,
                "target": target,
                "signal": signal_name,
                "state": state,
                "active_days": len(active_dates),
                "streak_days": streak,
                "last_seen": active_dates[-1],
            })

            if active_today:
                latest_rows.append({
                    "category": category,
                    "target": target,
                    "signal": signal_name,
                    "state": state,
                    "active_days": len(active_dates),
                    "streak_days": streak,
                    "last_seen": active_dates[-1],
                })

        timeline_rows = []
        for (category, target, signal_name), active_dates in signal_dates.items():
            for current_date in active_dates:
                timeline_rows.append({
                    "date": current_date,
                    "category": category,
                    "target": target,
                    "signal": signal_name,
                    "active": 1,
                })

        rows = sorted(
            rows,
            key=lambda r: (
                {"継続": 0, "新規": 1, "消滅": 2}.get(r["state"], 9),
                -r["streak_days"],
                -r["active_days"],
                r["target"],
            ),
        )

        return {
            "rows": rows,
            "timeline": pd.DataFrame(timeline_rows),
            "latest": latest_rows,
        }

    def _detect_market_signals(
        self,
        today_summary: dict,
        market_trend_df: pd.DataFrame,
    ) -> list[FlowSignal]:
        signals: list[FlowSignal] = []
        breakdown = today_summary.get("market_breakdown", {})
        ratios = _calc_breakdown_ratios(breakdown)
        market_ratio = today_summary.get("market_ratio", 0) or 0
        market_dod = today_summary.get("market_dod_change")
        t = self.thresholds

        if (
            market_ratio >= t.market_directional_min_pct
            and ratios["with_ratio"] >= t.market_directional_with_min_pct
        ):
            severity = (
                "high"
                if market_ratio >= t.market_warning_pct
                or ratios["with_ratio"] >= t.market_directional_with_high_pct
                else "medium"
            )
            signals.append(FlowSignal(
                category="市場全体",
                target="東証全体",
                signal="方向性売り警戒",
                severity=severity,
                rationale=(
                    f"総空売り{market_ratio:.1f}%、価格規制あり{ratios['with_ratio']:.1f}%。"
                    "価格規制あり主導で日次フローの売り圧力が強い。"
                ),
                watch_point=(
                    f"総空売り{t.market_warning_pct:.0f}%超、"
                    "または価格規制あり比率の続伸を確認する。"
                ),
                details=[
                    _threshold_detail("総空売り比率", market_ratio, t.market_directional_min_pct),
                    _threshold_detail("価格規制あり比率", ratios["with_ratio"], t.market_directional_with_min_pct),
                    _threshold_detail("高重要度ライン", market_ratio, t.market_warning_pct),
                ],
                invalidation_condition=(
                    f"総空売り比率が{t.market_directional_min_pct:.0f}%未満、"
                    f"または価格規制あり比率が{t.market_directional_with_min_pct:.0f}%未満へ低下。"
                ),
            ))

        if (
            ratios["without_share"] >= t.no_restriction_share_warning_pct
            or ratios["without_ratio"] >= t.no_restriction_ratio_warning_pct
        ):
            signals.append(FlowSignal(
                category="市場全体",
                target="東証全体",
                signal="ヘッジ・裁定混入警戒",
                severity="medium",
                rationale=(
                    f"価格規制なし比率{ratios['without_ratio']:.1f}%、"
                    f"規制なし構成比{ratios['without_share']:.1f}%。"
                    "弱気売りではなく裁定・ヘッジフローが混入している可能性がある。"
                ),
                watch_point=(
                    f"規制なし構成比{t.no_restriction_share_warning_pct:.0f}%超が続くか、"
                    "指数ETF/先物主導の動きと照合する。"
                ),
                details=[
                    _threshold_detail("価格規制なし比率", ratios["without_ratio"], t.no_restriction_ratio_warning_pct),
                    _threshold_detail("規制なし構成比", ratios["without_share"], t.no_restriction_share_warning_pct),
                ],
                invalidation_condition=(
                    f"規制なし構成比が{t.no_restriction_share_warning_pct:.0f}%未満、"
                    f"かつ価格規制なし比率が{t.no_restriction_ratio_warning_pct:.0f}%未満へ低下。"
                ),
            ))

        if (
            market_dod is not None
            and market_ratio >= t.market_cover_ratio_min_pct
            and market_dod <= t.cover_dod_drop_pt
        ):
            signals.append(FlowSignal(
                category="市場全体",
                target="東証全体",
                signal="ショートカバー候補",
                severity="medium",
                rationale=(
                    f"総空売り{market_ratio:.1f}%の高水準から前日比{market_dod:+.1f}pt低下。"
                    "売り圧力の一部後退、または買い戻しフローの可能性がある。"
                ),
                watch_point="翌営業日も低下が続くか、価格規制あり比率の低下を確認する。",
                details=[
                    _threshold_detail("総空売り比率", market_ratio, t.market_cover_ratio_min_pct),
                    _drop_detail("前日比", market_dod, t.cover_dod_drop_pt),
                    _value_detail("価格規制あり比率", ratios["with_ratio"]),
                ],
                invalidation_condition=(
                    "翌営業日に総空売り比率または価格規制あり比率が再上昇し、"
                    "前日比低下が継続しない。"
                ),
            ))

        other = next(
            (s for s in today_summary.get("sector_data", []) if s.get("s33_code") == "9999"),
            None,
        )
        if other:
            other_ratios = _calc_breakdown_ratios(other)
            market_total_volume = breakdown.get("total_volume_va", 0) or 0
            other_volume = other.get("total_volume_va", 0) or 0
            volume_share = other_volume / market_total_volume * 100 if market_total_volume else 0
            if (
                volume_share >= t.other_volume_share_warning_pct
                or other_ratios["without_share"] >= t.no_restriction_share_warning_pct
            ):
                signals.append(FlowSignal(
                    category="その他",
                    target="その他（33業種外）",
                    signal="その他主導注意",
                    severity="medium",
                    rationale=(
                        f"市場売買代金シェア{volume_share:.1f}%、"
                        f"規制なし構成比{other_ratios['without_share']:.1f}%。"
                        "ETF・REIT等の指数ヘッジ/裁定フローが市場全体を歪める可能性。"
                    ),
                    watch_point="その他カテゴリの売買代金シェアと規制なし構成比の上昇継続を確認する。",
                    details=[
                        _threshold_detail("市場売買代金シェア", volume_share, t.other_volume_share_warning_pct),
                        _threshold_detail("規制なし構成比", other_ratios["without_share"], t.no_restriction_share_warning_pct),
                    ],
                    invalidation_condition=(
                        f"市場売買代金シェアが{t.other_volume_share_warning_pct:.0f}%未満、"
                        f"かつ規制なし構成比が{t.no_restriction_share_warning_pct:.0f}%未満へ低下。"
                    ),
                ))

        return signals

    def _detect_sector_signals(self, today_summary: dict) -> list[FlowSignal]:
        signals: list[FlowSignal] = []
        t = self.thresholds

        for sector in today_summary.get("sector_data", []):
            name = sector.get("sector_name", "")
            ratio = sector.get("short_ratio_pct", 0) or 0
            dod = sector.get("dod_change")
            ratios = _calc_breakdown_ratios(sector)

            if (
                ratio >= t.sector_directional_min_pct
                and ratios["with_ratio"] >= t.sector_directional_with_min_pct
            ):
                severity = "high" if ratio >= t.sector_directional_high_pct else "medium"
                signals.append(FlowSignal(
                    category="業種",
                    target=name,
                    signal="方向性売り警戒",
                    severity=severity,
                    rationale=(
                        f"総空売り{ratio:.1f}%、価格規制あり{ratios['with_ratio']:.1f}%。"
                        "ヘッジより方向性売り寄りの圧力が強い。"
                    ),
                    watch_point="価格規制あり比率が低下するまで安易な逆張りを避ける。",
                    details=[
                        _threshold_detail("総空売り比率", ratio, t.sector_directional_min_pct),
                        _threshold_detail("価格規制あり比率", ratios["with_ratio"], t.sector_directional_with_min_pct),
                        _threshold_detail("高重要度ライン", ratio, t.sector_directional_high_pct),
                    ],
                    invalidation_condition=(
                        f"総空売り比率が{t.sector_directional_min_pct:.0f}%未満、"
                        f"または価格規制あり比率が{t.sector_directional_with_min_pct:.0f}%未満へ低下。"
                    ),
                ))

            if (
                ratios["without_share"] >= t.no_restriction_share_warning_pct
                or ratios["without_ratio"] >= t.no_restriction_ratio_warning_pct
            ):
                signals.append(FlowSignal(
                    category="業種",
                    target=name,
                    signal="ヘッジ・裁定混入警戒",
                    severity="medium",
                    rationale=(
                        f"価格規制なし比率{ratios['without_ratio']:.1f}%、"
                        f"規制なし構成比{ratios['without_share']:.1f}%。"
                        "裁定・ヘッジ由来の一時的フローが比率を押し上げている可能性。"
                    ),
                    watch_point="総空売り比率だけでなく規制なし構成比の反落を確認する。",
                    details=[
                        _threshold_detail("価格規制なし比率", ratios["without_ratio"], t.no_restriction_ratio_warning_pct),
                        _threshold_detail("規制なし構成比", ratios["without_share"], t.no_restriction_share_warning_pct),
                    ],
                    invalidation_condition=(
                        f"規制なし構成比が{t.no_restriction_share_warning_pct:.0f}%未満、"
                        f"かつ価格規制なし比率が{t.no_restriction_ratio_warning_pct:.0f}%未満へ低下。"
                    ),
                ))

            if (
                dod is not None
                and ratio >= t.sector_cover_ratio_min_pct
                and dod <= t.cover_dod_drop_pt
            ):
                signals.append(FlowSignal(
                    category="業種",
                    target=name,
                    signal="ショートカバー候補",
                    severity="medium",
                    rationale=(
                        f"高めの総空売り{ratio:.1f}%から前日比{dod:+.1f}pt低下。"
                        "一部買い戻し、または売り圧力後退の可能性。"
                    ),
                    watch_point="翌営業日の価格規制あり比率低下と株価反応を確認する。",
                    details=[
                        _threshold_detail("総空売り比率", ratio, t.sector_cover_ratio_min_pct),
                        _drop_detail("前日比", dod, t.cover_dod_drop_pt),
                        _value_detail("価格規制あり比率", ratios["with_ratio"]),
                    ],
                    invalidation_condition=(
                        "翌営業日に総空売り比率または価格規制あり比率が再上昇し、"
                        "前日比低下が継続しない。"
                    ),
                ))

            if (
                dod is not None
                and ratio <= t.sector_exhaustion_ratio_max_pct
                and dod <= t.exhaustion_dod_drop_pt
            ):
                signals.append(FlowSignal(
                    category="業種",
                    target=name,
                    signal="売り枯れ候補",
                    severity="low",
                    rationale=(
                        f"総空売り{ratio:.1f}%の低水準で前日比{dod:+.1f}pt低下。"
                        "日次フロー上は売り圧力が薄くなっている可能性。"
                    ),
                    watch_point="低空売りが継続するか、出来高低下によるノイズかを確認する。",
                    details=[
                        _upper_threshold_detail("総空売り比率", ratio, t.sector_exhaustion_ratio_max_pct),
                        _drop_detail("前日比", dod, t.exhaustion_dod_drop_pt),
                    ],
                    invalidation_condition=(
                        f"総空売り比率が{t.sector_exhaustion_ratio_max_pct:.0f}%を上回る、"
                        "または前日比低下が止まる。"
                    ),
                ))

        return _dedupe_and_limit(signals)


def _calc_breakdown_ratios(row: dict) -> dict[str, float]:
    total_volume = row.get("total_volume_va", 0) or 0
    short_with = row.get("shrt_with_res_va", 0) or 0
    short_without = row.get("shrt_no_res_va", 0) or 0
    total_short = row.get("total_short_va", short_with + short_without) or 0

    return {
        "with_ratio": short_with / total_volume * 100 if total_volume else 0,
        "without_ratio": short_without / total_volume * 100 if total_volume else 0,
        "without_share": short_without / total_short * 100 if total_short else 0,
    }


def _threshold_detail(label: str, value: float, threshold: float) -> str:
    diff = value - threshold
    return f"{label}: {value:.1f}% / 判定基準 {threshold:.1f}% / 超過幅 {diff:+.1f}pt"


def _upper_threshold_detail(label: str, value: float, threshold: float) -> str:
    diff = threshold - value
    return f"{label}: {value:.1f}% / 判定基準 {threshold:.1f}%以下 / 下回り幅 {diff:+.1f}pt"


def _drop_detail(label: str, value: float, threshold: float) -> str:
    margin = threshold - value
    return f"{label}: {value:+.1f}pt / 判定基準 {threshold:.1f}pt以下 / 判定余裕 {margin:+.1f}pt"


def _value_detail(label: str, value: float) -> str:
    return f"{label}: {value:.1f}%"


def _dedupe_and_limit(signals: list[FlowSignal], limit: int = 12) -> list[FlowSignal]:
    severity_order = {"high": 0, "medium": 1, "low": 2}
    seen: set[tuple[str, str]] = set()
    result = []

    for signal in sorted(signals, key=lambda s: severity_order.get(s.severity, 9)):
        key = (signal.target, signal.signal)
        if key in seen:
            continue
        seen.add(key)
        result.append(signal)
        if len(result) >= limit:
            break

    return result


def _calc_streak(ordered_dates: list[str], active_dates: set[str], latest_date: str) -> int:
    streak = 0
    for current_date in reversed(ordered_dates):
        if current_date > latest_date:
            continue
        if current_date not in active_dates:
            break
        streak += 1
    return streak
