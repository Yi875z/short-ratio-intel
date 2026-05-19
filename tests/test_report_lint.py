from src.ai_engine.report_lint import lint_report_markdown


def test_lint_detects_overconfidence():
    issues = lint_report_markdown("機関投資家による確信的な方向性売り。")

    assert issues
    assert issues[0].code == "overconfidence"


def test_lint_detects_unverified_market_data_when_not_in_input():
    issues = lint_report_markdown(
        "VIXが30を突破し、売り圧力が加速している。",
        input_text="東証全体の空売り比率は43%。",
    )

    assert any(issue.code == "unverified_market_data" for issue in issues)


def test_lint_allows_unverified_terms_in_checklist_context():
    issues = lint_report_markdown(
        "追加で見るべきデータ: VIX、WTI、SOX。",
        input_text="東証全体の空売り比率は43%。",
    )

    assert not issues
