from src.ai_engine import prompt_builder


def test_build_theme_transition_context_uses_saved_current_snapshot(monkeypatch):
    monkeypatch.setattr(
        prompt_builder,
        "get_market_theme_snapshot_dates",
        lambda limit=30: ["2026-05-17", "2026-05-18"],
    )

    def fake_get_snapshots(date):
        if date == "2026-05-18":
            return [{"key": "rates", "name": "米金利", "score": 5.0, "status": "主テーマ候補"}]
        if date == "2026-05-17":
            return [{"key": "rates", "name": "米金利", "score": 3.0, "status": "監視候補"}]
        return []

    monkeypatch.setattr(prompt_builder, "get_market_theme_snapshots", fake_get_snapshots)

    context = prompt_builder.build_theme_transition_context_for_prompt(
        "2026-05-18",
        today_summary={},
    )

    assert "現在テーマの取得元: saved_snapshot" in context
    assert "比較基準日: 2026-05-17" in context
    assert "[強化] 米金利" in context


def test_build_theme_transition_context_generates_current_when_missing(monkeypatch):
    monkeypatch.setattr(
        prompt_builder,
        "get_market_theme_snapshot_dates",
        lambda limit=30: ["2026-05-17"],
    )
    monkeypatch.setattr(
        prompt_builder,
        "get_market_theme_snapshots",
        lambda date: [{"key": "energy", "name": "原油", "score": 4.0}]
        if date == "2026-05-17"
        else [],
    )
    monkeypatch.setattr(
        prompt_builder,
        "build_theme_snapshot_dicts",
        lambda target_date, today_summary, manual_news="", baseline_context="": [
            {"key": "semis", "name": "半導体", "score": 3.0}
        ],
    )

    context = prompt_builder.build_theme_transition_context_for_prompt(
        "2026-05-18",
        today_summary={},
        current_news_text="SOXが弱い",
    )

    assert "現在テーマの取得元: generated_for_prompt_only" in context
    assert "[新規] 半導体" in context
    assert "[消滅] 原油" in context
