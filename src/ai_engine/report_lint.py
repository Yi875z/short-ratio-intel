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
]


def lint_report_markdown(
    markdown: str,
    input_text: str = "",
) -> list[ReportLintIssue]:
    """レポート本文に危険な表現がないか確認する。"""
    issues: list[ReportLintIssue] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        for pattern in FORBIDDEN_CERTAINTY_PATTERNS:
            if pattern in stripped:
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
