"""
AIレポート（ReadingReport）を画面・保存用の Markdown に描画する。

AI に書かせる欄と、毎日同じ文面になる定型（投資判断ガードレール・冒頭の注記）を分けている。
定型は AI に書かせると言い回しが揺れ、字数と生成時間を毎日消費するうえ、
品質チェックの「売買推奨ではない」「日次フロー」が偶然に左右される。ここで固定表示すれば必ず出る。
"""
from __future__ import annotations

from src.ai_engine.output_schema import ReadingReport, SectorAnalysis

HEADER_NOTE = (
    "> 注: 本レポートの空売り比率はJPX日次売買代金フローであり、空売り残高・建玉ではありません。"
)

# 投資判断ガードレール（固定文）。品質チェックの必須語（売買推奨ではなく／日次フロー／
# 反証条件／未確認）をすべて含む。文面を変えるときは report_quality の必須語も確認すること。
STATIC_GUARDRAILS = [
    "本レポートは売買推奨ではなく、JPXの日次フローを使った需給分析の補助材料です。",
    "空売り比率は日次フローであり、空売り残高・建玉ではありません。単独で売買判断をしないでください。",
    "上の確認条件と反証条件を翌営業日以降のデータで確かめてから解釈を採用してください。",
    "未確認データに挙げた指標は、事実として扱わないでください。",
]


def render_report_markdown(report: ReadingReport, date: str) -> str:
    lines = [
        "# 📊 空売り比率 完全解読レポート",
        f"## 〜 33業種分析×マクロ統合〜 {date}",
        "",
        HEADER_NOTE,
        "",
        "---",
        "",
        "## 🧭 本日の結論",
    ]
    if report.executive_summary:
        lines += [report.executive_summary, ""]
    if report.regime:
        lines += [f"**レジーム判定**: {report.regime}", ""]

    lines += [
        "## 🌍 現在の支配的マクロ背景",
        report.current_macro_context or "（未生成）",
        "",
        "---",
        "",
        "## ⚖️ 需給レジーム（比率・絶対額・流動性・価格反応）",
        report.supply_demand_regime_analysis,
        "",
        "## 🧭 JPX空売り内訳分析（価格規制あり/なし・その他）",
        report.jpx_short_selling_breakdown_analysis,
        "",
        "## 🗓️ 市場イベント文脈",
        report.event_calendar_context,
        "",
        "---",
        "",
        "## 🧭 市場テーマ判定",
    ]

    if report.dominant_market_themes:
        for theme in report.dominant_market_themes:
            lines += [
                f"### {theme.theme_name}",
                f"- **重要度**: {theme.importance} / **状態**: {theme.status} / "
                f"**フロー区分**: {theme.flow_classification}",
                f"- **関連業種**: {', '.join(theme.related_sectors)}",
                f"- **空売り比率との整合性**: {theme.short_ratio_alignment}",
                f"- **根拠**: {' / '.join(theme.evidence)}",
                f"- **注記**: {theme.caveat}",
                "",
            ]
    else:
        lines += ["市場テーマ判定は未生成です。", ""]

    lines += ["### テーマ転換シグナル", report.theme_shift_analysis, ""]
    if report.unverified_market_data:
        lines += ["### 未確認データ"]
        lines += [f"- {item}" for item in report.unverified_market_data]
        lines.append("")

    lines += ["---", "", "## 🔴 高空売りゾーン 注目業種"]
    lines += _sector_lines(report.top_sectors_analysis)
    lines += ["## 🟢 低空売りゾーン 注目業種"]
    lines += _sector_lines(report.low_sectors_analysis)

    lines += [
        "---",
        "",
        "## 🚨 シグナル履歴分析",
        f"- **継続**: {report.persistent_signal_summary}",
        f"- **新規**: {report.new_signal_summary}",
        f"- **消滅・弱体化**: {report.faded_signal_summary}",
        "",
        "## ⚔️ Retail Trap vs Pro Intent",
        f"- **🪤 Retail Trap（素人の罠）**: {report.retail_trap or '（未生成）'}",
        f"- **🎯 Pro Intent（機関の狙い・推測）**: {report.pro_intent or '（未生成）'}",
        f"- **🏦 投資主体別フローとの整合性**: {report.institutional_flow_alignment}",
        "",
        "---",
        "",
        "## ✅ 翌営業日の確認条件",
    ]
    lines += [f"- {item}" for item in report.confirmation_conditions] or ["- （未生成）"]
    lines += ["", "## ⚠️ 反証条件・誤判定しやすいケース"]
    lines += [f"- {item}" for item in report.false_positive_risks] or ["- （未生成）"]
    lines += ["", "## 🛡️ 投資判断ガードレール"]
    lines += [f"- {item}" for item in STATIC_GUARDRAILS]

    return "\n".join(lines)


def _sector_lines(sectors: list[SectorAnalysis]) -> list[str]:
    if not sectors:
        return ["（未生成）", ""]
    lines: list[str] = []
    for s in sectors:
        head = f"- **{s.sector_name}（{s.short_ratio_pct:.1f}%）**"
        tags = " / ".join(t for t in (s.zone_label, s.quadrant) if t)
        if tags:
            head += f" {tags}"
        lines += [head, f"  {s.interpretation}"]
    lines.append("")
    return lines
