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


def test_overwriting_a_report_records_the_new_writer(tmp_path, monkeypatch):
    """作り直したら書き手（model_used）も新しいものになる。

    2026-10-01、19:07 に Gemini 3.7 が書き 20:30 に dots 版へ差し替えた 10/1 が、
    本文は dots なのに model_used=gemini-3.7-flash のまま残っていた。
    """
    from sqlalchemy import create_engine

    from src.storage import db
    from src.storage.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "REPORTS_DIR", tmp_path / "reports")

    db.save_ai_report("2026-10-01", "Gemini の背景", "# Gemini", model_used="gemini-3.7-flash")
    db.save_ai_report("2026-10-01", "dots の背景", "# dots", model_used="chatgpt-dots")

    saved = db.get_ai_report("2026-10-01")
    assert saved.model_used == "chatgpt-dots"
    assert saved.macro_context == "dots の背景"
    assert saved.report_markdown == "# dots"
