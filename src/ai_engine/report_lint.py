"""
Geminiレポートの過剰断定・未確認データ断定を検出する軽量lint。
"""
from __future__ import annotations

import re
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
    # 「確信的な弱気売りを識別できない」のような否定（dots の 9/24 レポートで誤検知した）
    "できない",
    "とは言えない",
    "と読めない",
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


# ── 2026-09-30 追加。いずれも同日のレポートで実際に出た表現 ──
# 空売り比率は日次フロー。残高・建玉の語彙で書くと「売りが溜まっている」と誤読させる。
# （例:「価格規制ありの残高が高水準で残っており」）
BALANCE_TERMS = ["残高", "建玉"]
# 否定・注意書きの文脈なら許す（冒頭注記「空売り残高・建玉ではありません」等）。
BALANCE_NEGATION_MARKERS = [
    "ではありません", "ではない", "と表現しない", "とは表現しない",
    "誤解", "混同", "と読まない", "とは異なる",
]
# 空売りフローを「残高」と言い換えたときだけ検出する。信用残・貸借・空売り残高報告・
# オプションの建玉残高は実在するポジション側のデータ名で、むしろ見に行くべきもの
# （独立レビュー 2026-09-30 #5: 「信用取引残高」「Strike別建玉残高」を high で誤検知していた）。
BALANCE_FLOW_SUBJECTS = ["空売り", "価格規制", "ショート", "売り方"]
BALANCE_ALLOWED_DATA_NAMES = ["信用", "貸借", "残高報告", "0.5%", "Strike", "オプション", "先物"]
# 「空売り残高が未確認」「残高データによる確認」のように、別データとして確認を求める文脈は正しい使い方
# （ChatGPT 生成の検証 2026-10-01 で誤検知した）。
BALANCE_ALLOWED_CONTEXT = ["未確認", "確認", "残高データ", "データ"]

# 誇張。機械判定が NEUTRAL・確信度 low の日に「ベアからブルへ完全に反転」「流動性津波」と書いていた。
HYPERBOLE_TERMS = ["完全に", "壊滅", "歴史的", "津波", "確実に", "間違いなく", "必至"]
# 「〜と断定しない」「〜とは限らない」のような否定の文脈なら許す
HYPERBOLE_NEGATION_MARKERS = ["断定しない", "断定できない", "断定は避け", "禁止", "ではない", "とは限らない", "避ける"]

# 入力に無い数値を「想定」で補う（例:「前日（36.5%想定）」）。
FABRICATED_NUMBER_PATTERN = re.compile(
    r"\d[\d,.]*\s*(?:%|％|pt|円|兆円|億円|百万円)?\s*(?:想定|と仮定|と推定)"
)

# テクニカル指標は入力に無い。売買の撤退ラインを作る材料になる（例:「25日移動平均線割れで撤退」）。
# DATA_TERMS より厳しく扱い、「場合」「条件」などの言い回しでは許さない。
TECHNICAL_TERMS = ["移動平均", "25日線", "75日線", "RSI", "MACD", "ボリンジャー", "一目均衡表"]

# 機械判定の判定名を入力から拾う（例:「判定: THIN_MARKET（薄商い…） / 確信度: low」）
REGIME_PATTERN = re.compile(r"判定:\s*([A-Z_\-]+)（([^）]+)）")
THIN_MARKET_CONTRADICTIONS = ["売り圧力が強", "売り圧力の強", "売り圧力が高ま", "売り圧力が増"]

# 買い戻し（ショートカバー）の断定。空売り比率の低下は新規の空売りが減ったことで、
# 買い戻しの証拠ではない（ナレッジ29 §2）。新形式の最初のレポート（2026-10-01 検証）でも
# 「空売りの新規手控えと買い戻しが強まった」「ショートカバーが入った」が残っていた。
SHORT_COVER_ASSERTION = re.compile(
    r"(買い戻し|買戻し|ショートカバー|踏み上げ)[^。、]{0,6}(が|を)?(入った|強まった|進んだ|発生した|起きた|加速した|誘発)"
)
SHORT_COVER_HEDGES = ["候補", "可能性", "推測", "かもしれ", "とは言えない", "ではない", "要確認", "確認できない"]

# 投資主体別データに【鮮度注意】が付いた日に、それを裏付けとして使った文。
STALE_FLOW_MARKER = "【鮮度注意】"
FLOW_SUBJECT_TERMS = ["投資主体", "海外投資家", "主体別"]
EVIDENCE_TERMS = ["裏付け", "整合的", "一致している", "確認できる"]


def lint_report_markdown(
    markdown: str,
    input_text: str = "",
) -> list[ReportLintIssue]:
    """レポート本文に危険な表現がないか確認する。"""
    issues = _lint_lines(markdown, input_text)
    issues += _lint_regime_consistency(markdown, input_text)
    return issues


def _lint_regime_consistency(markdown: str, input_text: str) -> list[ReportLintIssue]:
    """入力の機械判定とレポート本文が食い違っていないか（画面とレポートで結論が割れるのを防ぐ）。"""
    matched = REGIME_PATTERN.search(input_text or "")
    if not matched:
        return []
    code, label = matched.group(1), matched.group(2)
    issues: list[ReportLintIssue] = []
    if code not in markdown and label not in markdown:
        issues.append(ReportLintIssue(
            severity="medium",
            code="regime_not_referenced",
            message=f"機械判定レジーム（{code}／{label}）への言及がありません",
            line="",
        ))
    if code == "THIN_MARKET":
        for line in markdown.splitlines():
            if any(term in line for term in THIN_MARKET_CONTRADICTIONS):
                issues.append(ReportLintIssue(
                    severity="high",
                    code="regime_contradiction",
                    message="機械判定は THIN_MARKET（見かけの高比率）なのに売り圧力の強さを主張しています",
                    line=line.strip(),
                ))
    return issues


def _lint_lines(markdown: str, input_text: str) -> list[ReportLintIssue]:
    issues: list[ReportLintIssue] = []
    current_section = ""
    stale_flow = STALE_FLOW_MARKER in (input_text or "")

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

        in_checklist = any(marker in current_section for marker in CHECKLIST_SECTION_MARKERS)

        for term in BALANCE_TERMS:
            if (
                term in stripped
                and not in_checklist
                and any(s in stripped for s in BALANCE_FLOW_SUBJECTS)
                and not any(a in stripped for a in BALANCE_ALLOWED_DATA_NAMES)
                and not any(c in stripped for c in BALANCE_ALLOWED_CONTEXT)
                and not any(m in stripped for m in BALANCE_NEGATION_MARKERS)
            ):
                issues.append(ReportLintIssue(
                    severity="high",
                    code="flow_as_balance",
                    message=f"日次フローを残高・建玉の語彙で記述: {term}",
                    line=stripped,
                ))
                break

        for term in HYPERBOLE_TERMS:
            if term in stripped and not any(m in stripped for m in HYPERBOLE_NEGATION_MARKERS):
                issues.append(ReportLintIssue(
                    severity="medium",
                    code="hyperbole",
                    message=f"誇張・過剰確信の表現: {term}",
                    line=stripped,
                ))
                break

        if (
            not in_checklist
            and SHORT_COVER_ASSERTION.search(stripped)
            and not any(h in stripped for h in SHORT_COVER_HEDGES)
        ):
            issues.append(ReportLintIssue(
                severity="medium",
                code="short_cover_asserted",
                message="買い戻し・踏み上げを断定（空売り比率だけではポジションの解消は分からない）",
                line=stripped,
            ))

        if FABRICATED_NUMBER_PATTERN.search(stripped):
            issues.append(ReportLintIssue(
                severity="high",
                code="fabricated_number",
                message="入力に無い数値を「想定・仮定」で補っている可能性",
                line=stripped,
            ))

        if not in_checklist:
            for term in TECHNICAL_TERMS:
                if term in stripped and term not in (input_text or ""):
                    issues.append(ReportLintIssue(
                        severity="medium",
                        code="technical_not_in_input",
                        message=f"入力に無いテクニカル指標に依拠: {term}",
                        line=stripped,
                    ))
                    break

        if (
            stale_flow
            # 確認条件の欄は「揃ったデータが得られたら再評価する」と書く場所で、裏付けには使っていない
            # （dots の 9/24 レポートで誤検知した）
            and not in_checklist
            and any(t in stripped for t in FLOW_SUBJECT_TERMS)
            and any(t in stripped for t in EVIDENCE_TERMS)
            and "未確認" not in stripped
        ):
            issues.append(ReportLintIssue(
                severity="medium",
                code="stale_flow_as_evidence",
                message="鮮度注意の付いた投資主体別データを裏付けに使っています",
                line=stripped,
            ))

    return issues
