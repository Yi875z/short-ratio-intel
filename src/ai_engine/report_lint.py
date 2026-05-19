"""
Geminiレポートの過剰断定・未確認データ断定を検出する軽量lint。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportLintIssue:
    severity: str
    code: str
    message: str
    line: str


FORBIDDEN_CERTAINTY_PATTERNS = [
    "確信的",
    "必ず上がる",
    "必ず下がる",
    "ショートスクイーズ確定",
    "反発確率",
    "勝率",
]

DATA_TERMS_REQUIRING_INPUT = [
    "WTI",
    "ブレント",
    "VIX",
    "日経VI",
    "SOX",
    "GEX",
    "CVD",
    "米10年",
    "米2年",
    "ドル円",
]

CAUTION_CONTEXT_MARKERS = [
    "未確認",
    "確認",
    "追加で見るべき",
    "監視ポイント",
    "データなし",
    "取得",
    "不確か",
    "可能性",
    "場合",
    "見るべき",
    "推移",
    "相関",
    "維持",
    "条件",
    "リスク注意",
]

CERTAINTY_CAUTION_MARKERS = CAUTION_CONTEXT_MARKERS + [
    "断定しない",
    "断定できない",
    "断定は避け",
    "禁止",
    "ではない",
    "とは限らない",
]

CHECKLIST_SECTION_MARKERS = [
    "追加で見るべきデータ",
    "次の監視ポイント",
    "翌営業日の確認条件",
    "監視ポイント",
    "確認条件",
    "未確認データ",
]


def lint_report_markdown(
    markdown: str,
    input_text: str = "",
) -> list[ReportLintIssue]:
    """レポート本文に危険な表現がないか確認する。"""
    issues: list[ReportLintIssue] = []
    current_section = ""

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            current_section = stripped

        for pattern in FORBIDDEN_CERTAINTY_PATTERNS:
            if pattern in stripped:
                if any(marker in stripped for marker in CERTAINTY_CAUTION_MARKERS):
                    continue
                issues.append(
                    ReportLintIssue(
                        severity="high",
                        code="overconfidence",
                        message=f"過剰断定表現を検出: {pattern}",
                        line=stripped,
                    )
                )

        for term in DATA_TERMS_REQUIRING_INPUT:
            if term not in stripped:
                continue
            if term in input_text:
                continue
            if any(marker in current_section for marker in CHECKLIST_SECTION_MARKERS):
                continue
            if any(marker in stripped for marker in CAUTION_CONTEXT_MARKERS):
                continue
            issues.append(
                ReportLintIssue(
                    severity="medium",
                    code="unverified_market_data",
                    message=f"入力にない市場データの断定可能性: {term}",
                    line=stripped,
                )
            )

    return issues
