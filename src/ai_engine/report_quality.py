"""
AIレポートの品質チェックを画面表示用に集約する。
"""
from __future__ import annotations

import json
import re
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


def build_quality_feedback_prompt_block(
    quality: ReportQualitySummary,
    max_items: int = 8,
) -> str:
    """品質チェック結果を再生成用プロンプトの改善メモに変換する。"""
    failed_items = sorted(
        quality.failed_items,
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(item.severity, 9),
            item.category,
            item.check_name,
        ),
    )
    if not failed_items:
        return ""

    lines = [
        "【前回AIレポート品質チェックからの改善メモ】",
        "- 注意: 以下は前回保存レポートの改善指示。今回入力された最新データを優先し、未確認データを事実として断定しない。",
        f"- 前回品質判定: {quality.status_label} / スコア {quality.score_pct:.1f}% / 失敗項目 {len(failed_items)}件",
        "- 修正すべき項目:",
    ]

    for item in failed_items[:max_items]:
        lines.append(
            f"  - [{item.severity}] {item.category}/{item.check_name}: {item.message}"
        )
        if item.evidence:
            lines.append(f"    evidence: {_clip_single_line(item.evidence, 160)}")

    lines.extend([
        "- 再生成時の指示:",
        "  - high項目は必ず解消する。",
        "  - 市場テーマ履歴・転換メモの主要テーマ名と変化状態を `dominant_market_themes` と `theme_shift_analysis` に反映する。",
        "  - 過剰断定表現は条件付き表現へ置き換える。",
        "  - 投資判断ガードレール、反証条件、未確認データの区別を明記する。",
    ])
    return "\n".join(lines)


def build_quality_review_markdown(
    quality: ReportQualitySummary,
    report_date: str = "",
    quality_feedback: str = "",
) -> str:
    """品質チェック結果を保存・共有しやすいMarkdownにする。"""
    title_date = f" {report_date}" if report_date else ""
    lines = [
        f"# AIレポート品質レビュー{title_date}",
        "",
        "## サマリー",
        f"- 判定: {quality.status_label}",
        f"- スコア: {quality.score_pct:.1f}%",
        f"- 重大: {quality.high_count}件",
        f"- 要確認: {len(quality.failed_items)}件",
        "",
        "## 未通過項目",
    ]

    if not quality.failed_items:
        lines.append("- 未通過項目はありません。")
    else:
        for item in quality.failed_items:
            lines.extend([
                f"### {item.category} / {item.check_name}",
                f"- severity: {item.severity}",
                f"- message: {item.message}",
            ])
            if item.evidence:
                lines.append(f"- evidence: {_clip_single_line(item.evidence, 300)}")
            lines.append("")

    if quality_feedback:
        lines.extend([
            "",
            "## 再生成用改善メモ",
            "",
            quality_feedback,
        ])

    return "\n".join(lines).strip() + "\n"


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
    theme_transition_context: str = "",
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

    items.extend(_evaluate_theme_transition_reflection(
        markdown=markdown,
        data=data,
        theme_transition_context=theme_transition_context,
    ))

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


def _evaluate_theme_transition_reflection(
    markdown: str,
    data: dict[str, Any] | None,
    theme_transition_context: str,
) -> list[ReportQualityItem]:
    if not theme_transition_context:
        return []

    items: list[ReportQualityItem] = []
    transition_rows = _extract_transition_rows(theme_transition_context)
    if not transition_rows:
        items.append(ReportQualityItem(
            category="テーマ転換反映",
            check_name="転換メモ入力",
            passed=True,
            severity="medium",
            message="比較対象となるテーマ変化はありません。",
        ))
        return items

    searchable_text = markdown
    if data is not None:
        searchable_text += "\n" + json.dumps(data, ensure_ascii=False)

    priority_rows = transition_rows[:3]
    reflected_names = [
        row["name"]
        for row in priority_rows
        if row["name"] and row["name"] in searchable_text
    ]
    items.append(ReportQualityItem(
        category="テーマ転換反映",
        check_name="主要テーマ名反映",
        passed=bool(reflected_names),
        severity="medium",
        message=(
            "テーマ転換メモの主要テーマ名がレポートに反映されています。"
            if reflected_names
            else "テーマ転換メモの主要テーマ名がレポートに見つかりません。"
        ),
        evidence=", ".join(reflected_names) if reflected_names else _transition_evidence(priority_rows),
    ))

    non_continuation_states = {
        row["state"]
        for row in transition_rows
        if row["state"] and row["state"] != "継続"
    }
    shift_text = ""
    if data is not None:
        shift_text = str(data.get("theme_shift_analysis") or "")
    else:
        shift_text = markdown

    state_terms = (
        non_continuation_states
        if non_continuation_states
        else {"転換", "浮上", "後退", "関心移動"}
    )
    matched_terms = [term for term in state_terms if term and term in shift_text]
    items.append(ReportQualityItem(
        category="テーマ転換反映",
        check_name="テーマ転換分析反映",
        passed=bool(matched_terms),
        severity="medium",
        message=(
            "テーマ転換分析に変化状態が反映されています。"
            if matched_terms
            else "テーマ転換分析に新規・強化・弱体化・消滅などの変化状態が見つかりません。"
        ),
        evidence=", ".join(matched_terms) if matched_terms else _transition_evidence(priority_rows),
    ))

    return items


def _extract_transition_rows(theme_transition_context: str) -> list[dict[str, str]]:
    rows = []
    pattern = re.compile(r"^\s*-\s*\[(?P<state>[^\]]+)\]\s*(?P<name>[^:：]+)")
    for line in theme_transition_context.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        rows.append({
            "state": match.group("state").strip(),
            "name": match.group("name").strip(),
        })
    return rows


def _transition_evidence(rows: list[dict[str, str]]) -> str:
    return " / ".join(
        f"[{row.get('state', '')}] {row.get('name', '')}"
        for row in rows
    )


def _clip_single_line(text: str, max_chars: int) -> str:
    one_line = " ".join(str(text).split())
    if len(one_line) <= max_chars:
        return one_line
    return one_line[:max_chars] + "..."
