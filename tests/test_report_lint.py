from src.ai_engine.report_lint import lint_report_markdown


def test_lint_detects_overconfidence():
    issues = lint_report_markdown("機関投資家による確信的な方向性売り。")

    assert issues
    assert issues[0].code == "overconfidence"


def test_lint_allows_overconfidence_terms_in_caution_context():
    issues = lint_report_markdown("価格規制ありが高くても機関の確信的売りとは断定しない。")

    assert not issues


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


def test_lint_allows_unverified_terms_under_checklist_section():
    issues = lint_report_markdown(
        "## 追加で見るべきデータ\n- WTI原油先物および北海ブレントの価格推移",
        input_text="東証全体の空売り比率は43%。",
    )

    assert not issues


def _has_unverified(line: str) -> bool:
    return any(
        issue.code == "unverified_market_data"
        for issue in lint_report_markdown(line, input_text="")
    )


def test_lint_allows_impact_channel_framing():
    # (C) 影響経路・経由など「値を断定していない」説明は未確認断定としない
    assert _has_unverified("- 影響経路: ドル円為替レートの変動を通じた輸出株への影響") is False
    assert _has_unverified("SOX指数経由の日本半導体株への波及") is False


def test_lint_allows_speculation_framing():
    assert _has_unverified("日銀利上げ思惑でドル円の円安観測が銀行株を下支え") is False


def test_lint_still_flags_raw_data_assertion():
    # 報道・思惑・経路の枕詞が無い生の数値断定は引き続き検出する
    assert _has_unverified("WTIは95ドルで原油高が継続") is True
