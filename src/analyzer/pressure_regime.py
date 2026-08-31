"""
売り圧力レジームの判定。

## 設計方針

**単一の固定閾値だけで判定しない。** 「空売り比率が40%を超えたら売り圧力」の
ような絶対水準の一発判定は、平常時の水準が違う局面で同じ意味に読めない。
本モジュールは各指標を「自分自身の直近20営業日分布に対する相対位置（Zスコア）」
と「直近5営業日平均比」へ変換したうえで、**複数条件の充足**でレジームを決める。

判定は6つ。いずれも比率・絶対額・流動性・価格反応の組み合わせで定義する。

    SELL_PRESSURE          売り圧力: 空売りが実額でも増え、規制あり主導で、価格が下げている
    THIN_MARKET            薄商い: 商いが細って比率だけ高い。空売りの実額は増えていない
    ABSORPTION             吸収: 空売りが増えているのに価格が下がらない
    BROAD_DE-RISKING       全面リスク回避: 価格下落・広がりも悪化・商いは増加
    SHORT_COVER_CANDIDATE  買い戻し候補: 高水準からの比率低下・実額減・価格上昇
    NEUTRAL                上記いずれも成立しない、または判定材料が足りない

各レジームは「必要な入力」を宣言しており、入力が欠けている場合はそのレジームを
**候補から外す**（欠損を0とみなして誤判定しない）。たとえば騰落銘柄数が取れない日は
BROAD_DE-RISKING は判定されず、その旨が理由に残る。

## 確信度

充足した条件の数と、価格規制なし構成比の高さから決める。
規制なしが多い日は裁定・ヘッジ由来のフローが混ざっている可能性があるため、
方向性売り系の判定は確信度を1段下げる（判定自体は消さない）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from config.pressure_thresholds import (
    PRESSURE_THRESHOLDS,
    PressureThresholds,
)
from src.analyzer.pressure_metrics import PressureMetrics

REGIME_SELL_PRESSURE = "SELL_PRESSURE"
REGIME_THIN_MARKET = "THIN_MARKET"
REGIME_ABSORPTION = "ABSORPTION"
REGIME_BROAD_DE_RISKING = "BROAD_DE-RISKING"
REGIME_SHORT_COVER = "SHORT_COVER_CANDIDATE"
REGIME_NEUTRAL = "NEUTRAL"

REGIME_LABELS: dict[str, str] = {
    REGIME_SELL_PRESSURE: "売り圧力",
    REGIME_THIN_MARKET: "薄商い（見かけの高比率）",
    REGIME_ABSORPTION: "売り吸収",
    REGIME_BROAD_DE_RISKING: "全面リスク回避",
    REGIME_SHORT_COVER: "買い戻し候補",
    REGIME_NEUTRAL: "中立",
}

REGIME_DESCRIPTIONS: dict[str, str] = {
    REGIME_SELL_PRESSURE:
        "空売りが比率だけでなく実額でも増え、価格規制あり（方向性売り寄り）が主導し、"
        "価格も下げている。売り手が実弾を入れている状態。",
    REGIME_THIN_MARKET:
        "市場の商いが細っており、空売り比率の高さは分母の縮小によるもの。"
        "空売り代金自体は増えていないため、比率の高さを売り圧力と読むと誤る。",
    REGIME_ABSORPTION:
        "空売り代金が増えているのに価格が下がっていない。売りが買いに吸収されており、"
        "踏み上げに転じる余地がある。",
    REGIME_BROAD_DE_RISKING:
        "価格が下落し、値下がり銘柄が広範に広がり、商いも膨らんでいる。"
        "空売り主導というより現物売りを含む全面的なリスク回避。",
    REGIME_SHORT_COVER:
        "高水準だった空売り比率が明確に低下し、空売り代金も平均を下回り、価格が上昇している。"
        "買い戻し主導の可能性。",
    REGIME_NEUTRAL:
        "いずれのレジームも条件を満たさない、または判定に必要な入力が不足している。",
}


@dataclass(frozen=True)
class RegimeVerdict:
    """1つのレジームの判定結果。"""

    regime: str
    label: str
    matched: bool
    confidence: str                      # "high" | "medium" | "low" | "n/a"
    satisfied: tuple[str, ...] = ()      # 満たした条件（数値つき）
    unsatisfied: tuple[str, ...] = ()    # 満たさなかった条件（数値つき）
    missing_inputs: tuple[str, ...] = () # 欠けていて評価できなかった入力
    caveats: tuple[str, ...] = ()        # 解釈上の注意（確信度を下げた理由など）

    @property
    def evaluable(self) -> bool:
        return not self.missing_inputs

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RegimeResult:
    """対象日の総合判定。"""

    date: str
    primary: str
    primary_label: str
    description: str
    confidence: str
    reasons: tuple[str, ...] = ()
    also_matched: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    verdicts: tuple[RegimeVerdict, ...] = field(default=())

    def to_dict(self) -> dict:
        data = asdict(self)
        data["verdicts"] = [v.to_dict() for v in self.verdicts]
        return data


# ----------------------------------------------------------------------
# 条件の記述
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    """1つの判定条件。値が None なら「評価不能」として扱う。"""

    description: str          # 「総空売り代金Zが +1.0 以上」など
    input_name: str           # 欠損時に名前で報告するための入力名
    value: Optional[float]
    test: Callable[[float], bool]
    unit: str = ""

    def evaluate(self) -> Optional[bool]:
        if self.value is None:
            return None
        return self.test(self.value)

    def render(self) -> str:
        if self.value is None:
            return f"{self.description}（{self.input_name}が取得できず評価不能）"
        return f"{self.description}｜実測 {self.value:+.2f}{self.unit}"


def _ge(threshold: float) -> Callable[[float], bool]:
    return lambda value: value >= threshold


def _le(threshold: float) -> Callable[[float], bool]:
    return lambda value: value <= threshold


def _lt(threshold: float) -> Callable[[float], bool]:
    return lambda value: value < threshold


def _gt(threshold: float) -> Callable[[float], bool]:
    return lambda value: value > threshold


# ----------------------------------------------------------------------
# 判定本体
# ----------------------------------------------------------------------
class PressureRegimeClassifier:
    """需給指標から売り圧力レジームを判定する。"""

    def __init__(self, thresholds: PressureThresholds = PRESSURE_THRESHOLDS) -> None:
        self.thresholds = thresholds

    def classify(self, metrics: PressureMetrics) -> RegimeResult:
        verdicts = [
            self._judge_sell_pressure(metrics),
            self._judge_thin_market(metrics),
            self._judge_absorption(metrics),
            self._judge_broad_de_risking(metrics),
            self._judge_short_cover(metrics),
        ]

        matched = [v for v in verdicts if v.matched]
        matched.sort(key=lambda v: (_confidence_rank(v.confidence), -len(v.satisfied)))

        if not matched:
            return self._neutral(metrics, tuple(verdicts))

        primary = matched[0]
        return RegimeResult(
            date=metrics.date,
            primary=primary.regime,
            primary_label=primary.label,
            description=REGIME_DESCRIPTIONS[primary.regime],
            confidence=primary.confidence,
            reasons=primary.satisfied,
            also_matched=tuple(v.regime for v in matched[1:]),
            caveats=primary.caveats,
            missing_inputs=metrics.missing_inputs,
            verdicts=tuple(verdicts),
        )

    # ------------------------------------------------------------------
    def _judge_sell_pressure(self, m: PressureMetrics) -> RegimeVerdict:
        t = self.thresholds
        conditions = [
            Condition(
                f"総空売り代金のZスコアが {t.short_value_surge_z:+.1f} 以上（実額で増加）",
                "総空売り代金", m.short_value_change.zscore, _ge(t.short_value_surge_z),
            ),
            Condition(
                "総空売り代金が5日平均を上回る",
                "総空売り代金", m.short_value_change.vs_avg_pct, _gt(0.0), "%",
            ),
            Condition(
                f"価格規制あり比率のZスコアが {t.with_restriction_elevated_z:+.1f} 以上",
                "価格規制あり比率", m.with_ratio_change.zscore,
                _ge(t.with_restriction_elevated_z),
            ),
            Condition(
                "TOPIXが下落",
                "TOPIX騰落率", m.price.topix_change_pct, _lt(t.price_down_pct), "%",
            ),
        ]
        return self._build(REGIME_SELL_PRESSURE, conditions, m, dilution_sensitive=True)

    def _judge_thin_market(self, m: PressureMetrics) -> RegimeVerdict:
        """比率の高さが分母の縮小によるものかを切り分ける。

        依頼の核心のひとつ。空売り代金が増えていないのに比率が高い日を
        SELL_PRESSURE と読ませないための隔離レジーム。
        """
        t = self.thresholds
        thin = _first_not_none(
            _passes(m.market_volume_change.zscore, _le(t.volume_thin_z)),
            _passes(m.market_volume_change.vs_avg_pct, _le(t.volume_thin_vs_avg_pct)),
        )
        conditions = [
            Condition(
                f"市場売買代金のZスコアが {t.volume_thin_z:+.1f} 以下、"
                f"または5日平均比 {t.volume_thin_vs_avg_pct:+.0f}% 以下（商いが細い）",
                "市場売買代金",
                _pick_thin_value(m, t),
                lambda _value: bool(thin),
            ),
            Condition(
                f"空売り比率のZスコアが {t.ratio_elevated_z:+.1f} 以上（比率は高い）",
                "空売り比率", m.total_ratio_change.zscore, _ge(t.ratio_elevated_z),
            ),
            Condition(
                f"総空売り代金のZスコアが {t.short_value_quiet_z:+.1f} 未満（実額は増えていない）",
                "総空売り代金", m.short_value_change.zscore, _lt(t.short_value_quiet_z),
            ),
        ]
        return self._build(REGIME_THIN_MARKET, conditions, m)

    def _judge_absorption(self, m: PressureMetrics) -> RegimeVerdict:
        t = self.thresholds
        conditions = [
            Condition(
                f"総空売り代金のZスコアが {t.short_value_mild_z:+.1f} 以上（売りは出ている）",
                "総空売り代金", m.short_value_change.zscore, _ge(t.short_value_mild_z),
            ),
            Condition(
                "TOPIXが下落していない（売りが吸収されている）",
                "TOPIX騰落率", m.price.topix_change_pct, _ge(t.price_down_pct), "%",
            ),
        ]
        return self._build(REGIME_ABSORPTION, conditions, m)

    def _judge_broad_de_risking(self, m: PressureMetrics) -> RegimeVerdict:
        """空売り比率の高低は問わない。現物売りを含む全面安を拾う。

        騰落銘柄数が必須。取れない日はこのレジームを候補から外す。
        """
        t = self.thresholds
        conditions = [
            Condition(
                "TOPIXが下落",
                "TOPIX騰落率", m.price.topix_change_pct, _lt(t.price_down_pct), "%",
            ),
            Condition(
                f"ネットブレッドスが {t.breadth_broad_decline:+.2f} 以下（値下がりが広範）",
                "騰落銘柄数", m.breadth.net_breadth, _le(t.breadth_broad_decline),
            ),
            Condition(
                f"市場売買代金のZスコアが {t.volume_active_z:+.1f} 以上（商いは細っていない）",
                "市場売買代金", m.market_volume_change.zscore, _ge(t.volume_active_z),
            ),
        ]
        return self._build(REGIME_BROAD_DE_RISKING, conditions, m)

    def _judge_short_cover(self, m: PressureMetrics) -> RegimeVerdict:
        t = self.thresholds
        conditions = [
            Condition(
                f"空売り比率が前日から {t.cover_ratio_drop_pt:+.1f}pt 以上低下",
                "空売り比率", _ratio_dod_points(m), _le(t.cover_ratio_drop_pt), "pt",
            ),
            Condition(
                "総空売り代金が5日平均を下回る",
                "総空売り代金", m.short_value_change.vs_avg_pct,
                _lt(t.cover_short_value_vs_avg_pct), "%",
            ),
            Condition(
                "TOPIXが上昇",
                "TOPIX騰落率", m.price.topix_change_pct, _gt(t.price_up_pct), "%",
            ),
        ]
        return self._build(REGIME_SHORT_COVER, conditions, m)

    # ------------------------------------------------------------------
    def _build(
        self,
        regime: str,
        conditions: list[Condition],
        metrics: PressureMetrics,
        dilution_sensitive: bool = False,
    ) -> RegimeVerdict:
        satisfied: list[str] = []
        unsatisfied: list[str] = []
        missing: list[str] = []

        for condition in conditions:
            outcome = condition.evaluate()
            if outcome is None:
                missing.append(condition.input_name)
            elif outcome:
                satisfied.append(condition.render())
            else:
                unsatisfied.append(condition.render())

        # 入力が欠けていたら判定しない（欠損を0とみなして誤判定しない）
        if missing:
            return RegimeVerdict(
                regime=regime,
                label=REGIME_LABELS[regime],
                matched=False,
                confidence="n/a",
                satisfied=tuple(satisfied),
                unsatisfied=tuple(unsatisfied),
                missing_inputs=tuple(dict.fromkeys(missing)),
            )

        matched = not unsatisfied
        confidence = "n/a"
        caveats: list[str] = []

        if matched:
            confidence = "high" if len(satisfied) >= 3 else "medium"
            if dilution_sensitive:
                share = metrics.ratios.without_share_pct
                if share is not None and share >= self.thresholds.without_share_dilution_pct:
                    confidence = _downgrade(confidence)
                    caveats.append(
                        f"価格規制なしの構成比が {share:.1f}% と高く、"
                        "裁定・ヘッジ由来のフローが混ざっている可能性があるため確信度を下げた。"
                    )

        return RegimeVerdict(
            regime=regime,
            label=REGIME_LABELS[regime],
            matched=matched,
            confidence=confidence,
            satisfied=tuple(satisfied),
            unsatisfied=tuple(unsatisfied),
            caveats=tuple(caveats),
        )

    def _neutral(
        self, metrics: PressureMetrics, verdicts: tuple[RegimeVerdict, ...]
    ) -> RegimeResult:
        blocked = tuple(dict.fromkeys(
            name for v in verdicts for name in v.missing_inputs
        ))
        reasons = []
        if blocked:
            reasons.append(
                "判定に必要な入力が不足しているレジームがある: " + " / ".join(blocked)
            )
        reasons.append("成立したレジームなし。各条件の充足状況は明細を参照。")

        return RegimeResult(
            date=metrics.date,
            primary=REGIME_NEUTRAL,
            primary_label=REGIME_LABELS[REGIME_NEUTRAL],
            description=REGIME_DESCRIPTIONS[REGIME_NEUTRAL],
            confidence="low" if blocked else "medium",
            reasons=tuple(reasons),
            missing_inputs=metrics.missing_inputs,
            verdicts=verdicts,
        )


# ----------------------------------------------------------------------
def _ratio_dod_points(metrics: PressureMetrics) -> Optional[float]:
    """空売り比率の前日比を「%変化」ではなく「pt差」で返す。

    比率の変化は pt で見るのが実務の読み方（40%→37%は -3pt であって -7.5% ではない）。
    """
    change = metrics.total_ratio_change
    if change.latest is None or change.dod_pct is None:
        return None
    previous = change.latest / (1 + change.dod_pct / 100) if change.dod_pct != -100 else None
    if previous is None:
        return None
    return round(change.latest - previous, 2)


def _pick_thin_value(metrics: PressureMetrics, thresholds: PressureThresholds):
    """薄商い条件の表示用に、評価に使えた実測値を1つ選ぶ。"""
    if metrics.market_volume_change.zscore is not None:
        return metrics.market_volume_change.zscore
    return metrics.market_volume_change.vs_avg_pct


def _passes(value: Optional[float], test: Callable[[float], bool]) -> Optional[bool]:
    if value is None:
        return None
    return test(value)


def _first_not_none(*values: Optional[bool]) -> Optional[bool]:
    """いずれかが True なら True、すべて False なら False、全部 None なら None。"""
    seen = [v for v in values if v is not None]
    if not seen:
        return None
    return any(seen)


def _confidence_rank(confidence: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(confidence, 9)


def _downgrade(confidence: str) -> str:
    return {"high": "medium", "medium": "low"}.get(confidence, confidence)
