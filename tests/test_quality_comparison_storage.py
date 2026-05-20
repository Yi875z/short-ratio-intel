from src.storage import db


def test_save_and_load_ai_report_quality_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "REPORTS_DIR", tmp_path)

    path = db.save_ai_report_quality_comparison(
        "2026-05-18",
        "# AIレポート再生成 品質比較 2026-05-18\n",
    )

    assert path == tmp_path / "ai_report_quality_comparison_2026-05-18.md"
    assert db.load_ai_report_quality_comparison("2026-05-18").startswith(
        "# AIレポート再生成 品質比較"
    )
    assert db.load_ai_report_quality_comparison("2026-05-19") == ""
