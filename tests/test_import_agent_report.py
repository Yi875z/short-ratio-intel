"""外部エージェント（dots 等）のレポート取り込みの検証テスト（2026-10-01）。"""
import json

import pytest

from scripts.import_agent_report import AgentReportRejected, validate_agent_report


def _report(**overrides) -> str:
    data = {
        "executive_summary": "東証需給は ABSORPTION。売りを買いが吸収した。",
        "supply_demand_regime_analysis": "事実: ABSORPTION。",
        "jpx_short_selling_breakdown_analysis": "事実: 規制あり34.7%。",
        "theme_shift_analysis": "浮上中。",
        "top_sectors_analysis": [
            {"sector_name": n, "short_ratio_pct": 45.0, "interpretation": "i"} for n in ("a", "b", "c")
        ],
        "confirmation_conditions": ["a", "b", "c"],
        "false_positive_risks": ["a", "b"],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def test_valid_report_is_accepted_and_rendered():
    report, markdown, lint, quality = validate_agent_report(_report(), "2026-09-29")
    assert report.executive_summary.startswith("東証需給")
    assert "2026-09-29" in markdown
    assert "売買推奨ではなく" in markdown         # 固定ガードレールが付く


@pytest.mark.parametrize("raw, reason", [
    ("これはJSONではありません", "JSON として読めない"),
    ("{}", "本日の結論"),
    (_report(top_sectors_analysis=[]), "注目業種"),
])
def test_invalid_or_empty_report_is_rejected(raw, reason):
    """検証に通らない出力は画面に載せない（Gemini 経路の EmptyReportError と同じ基準）。"""
    with pytest.raises(AgentReportRejected, match=reason):
        validate_agent_report(raw, "2026-09-29")


def test_code_fenced_json_is_accepted():
    """エージェントがコードブロックで囲んで返しても読める。"""
    validate_agent_report("```json\n" + _report() + "\n```", "2026-09-29")
