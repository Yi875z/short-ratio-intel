"""
AIレポートの品質チェックを画面表示用に集約する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.ai_engine.report_lint import lint_report_markdown


@dataclass(frozen=True)
class ReportQualityItem:
    category: str
    check_name: str
    passed: bool
    severity: str
    message: str
    evidence: str = ""


@dataclass(frozen=True)
class ReportQualitySummary:
    items: list[ReportQualityItem]

    @property
    def failed_items(self) -> list[ReportQualityItem]:
        return [item for item in self.items if not item.passed]

    @property
    def high_count(self) -> int:
        return sum(1 for item in self.failed_items if item.severity == "high")

    @property
    def medium_count(self) -> int:
        return sum(1 for item in self.failed_items if item.severity == "medium")

    @property
    def score_pct(self) -> float:
        if not self.items:
            return 100.0
        return round((len(self.items) - len(self.failed_items)) / len(self.items) * 100, 1)

    @property
    def status_label(self) -> str:
        if self.high_count:
            return "要修正"
        if self.medium_count:
            return "要確認"
        if self.failed_items:
            return "軽微な確認"
        return "OK"

    def to_rows(self, include_passed: bool = False) -> list[dict]:
        rows = []
        for item in self.items:
            if item.passed and not include_passed:
                continue
            rows.append({
                "category": item.category,
                "check": item.check_name,
                "result": "OK" if item.passed else "要確認",
                "severity": item.severity,
                "message": item.message,
                "evidence": item.evidence,
            })
        return rows


REQUIRED_MARKDOWN_SECTIONS = [
    ("現在の支配的マクロ背景", "現在の支配的マクロ背景"),
    ("東証全体サマリー", "東証全体サマリー"),
    ("JPX空売り内訳分析", "JPX空売り内訳分析"),
    ("市場テーマ判定", "市場テーマ判定"),
    ("シグナル履歴分析", "シグナル履歴分析"),
    ("投資判断ガードレール", "投資判断ガードレール"),
    ("Retail Trap vs Pro Intent", "Retail Trap vs Pro Intent"),
    ("戦略的示唆", "戦略的示唆"),
    ("総括", "総括"),
    ("次の監視ポイント", "次の監視ポイント"),
]

REQUIRED_GUARDRAIL_TERMS = [
    ("日次フロー明記", ["日次フロー", "JPX日次売買代金フロー"]),
    ("売買推奨ではない", ["売買推奨ではなく", "売買推奨ではない"]),
    ("反証条件", ["反証条件", "確認条件"]),
    ("未確認データ区別", ["未確認", "追加で見るべきデータ"]),
]

REQUIRED_JSON_LIST_FIELDS = [
    ("dominant_market_themes", "市場テーマ候補", 1),
    ("investment_guardrails", "投資判断ガードレール", 3),
    ("confirmation_conditions", "翌営業日の確認条件", 3),
    ("false_positive_risks", "誤判定リスク", 3),
    ("additional_data_to_check", "追加確認データ", 3),
]


def evaluate_report_quality(
    markdown: str,
    report_json: str | None = "",
    input_text: str = "",
) -> ReportQualitySummary:
    """保存済みAIレポートの品質を軽量に評価する。"""
    items: list[ReportQualityItem] = []
    markdown = markdown or ""

    for label, token in REQUIRED_MARKDOWN_SECTIONS:
        passed = token in markdown
        items.append(ReportQualityItem(
            category="構成",
            check_name=label,
            passed=passed,
            severity="medium",
            message="必須セクションがあります。" if passed else "必須セクションが見つかりません。",
        ))

    for label, terms in REQUIRED_GUARDRAIL_TERMS:
        matched = [term for term in terms if term in markdown]
        passed = bool(matched)
        items.append(ReportQualityItem(
            category="ガードレール",
            check_name=label,
            passed=passed,
            severity="high" if label in {"日次フロー明記", "売買推奨ではない"} else "medium",
            message="安全表現を確認しました。" if passed else "安全表現が不足している可能性があります。",
            evidence=", ".join(matched),
        ))

    for issue in lint_report_markdown(markdown, input_text=input_text):
        items.append(ReportQualityItem(
            category="表現lint",
            check_name=issue.code,
            passed=False,
            severity=issue.severity,
            message=issue.message,
            evidence=issue.line,
        ))

    data = _parse_report_json(report_json)
    if data is None:
        items.append(ReportQualityItem(
            category="構造化JSON",
            check_name="JSON保存",
            passed=False,
            severity="medium",
            message="構造化JSONが保存されていないか、読み取れません。",
        ))
    else:
        items.append(ReportQualityItem(
            category="構造化JSON",
            check_name="JSON保存",
            passed=True,
            severity="medium",
            message="構造化JSONを確認しました。",
        ))
        items.extend(_evaluate_json_fields(data))

    return ReportQualitySummary(items=items)


def _evaluate_json_fields(data: dict[str, Any]) -> list[ReportQualityItem]:
    items: list[ReportQualityItem] = []
    for field_name, label, min_count in REQUIRED_JSON_LIST_FIELDS:
        value = data.get(field_name)
        count = len(value) if isinstance(value, list) else 0
        passed = count >= min_count
        items.append(ReportQualityItem(
            category="構造化JSON",
            check_name=label,
            passed=passed,
            severity="medium",
            message=(
                f"{count}件確認しました。"
                if passed
                else f"{min_count}件以上が望ましい項目です。現在{count}件です。"
            ),
            evidence=field_name,
        ))

    for field_name, label in [
        ("theme_shift_analysis", "テーマ転換分析"),
        ("theme_sector_alignment", "テーマと業種の整合性"),
    ]:
        value = str(data.get(field_name) or "").strip()
        passed = bool(value) and "未生成" not in value
        items.append(ReportQualityItem(
            category="構造化JSON",
            check_name=label,
            passed=passed,
            severity="medium",
            message="分析文を確認しました。" if passed else "分析文が未生成または不足しています。",
            evidence=field_name,
        ))

    return items


def _parse_report_json(report_json: str | None) -> dict[str, Any] | None:
    if not report_json:
        return None
    try:
        data = json.loads(report_json)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
