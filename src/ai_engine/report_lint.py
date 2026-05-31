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

# データ項目チェック専用の許容マーカー。報道引用・思惑・影響経路の説明など
# 「値を事実として断定していない」フレーミングは未確認断定とみなさない（(C)対応）。
DATA_CAUTION_MARKERS = CAUTION_CONTEXT_MARKERS + [
    "報道",
    "報道ベース",
    "思惑",
    "観測",
    "期待",
    "懸念",
    "見通し",
    "影響経路",
    "経由",
    "通じ",
    "に伴う",
    "背景",
    "とされ",
    "示唆",
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
            # 見出し行はセクション名・テーマ名・業種名のラベルであり数値の断定ではない。
            # 例: "### BOJ・ドル円・日本株バリュー/グロース" の「ドル円」を誤検知しない。
            current_section = stripped
            continue

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
            if any(marker in stripped for marker in DATA_CAUTION_MARKERS):
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
