from src.macro_context.theme_history import (
    build_theme_comparison_rows,
    build_theme_history_rows,
    build_theme_transition_prompt_block,
    find_previous_theme_date,
)


def test_find_previous_theme_date_returns_nearest_before_selected_date():
    dates = ["2026-05-16", "2026-05-18", "2026-05-20"]

    assert find_previous_theme_date(dates, "2026-05-19") == "2026-05-18"
    assert find_previous_theme_date(dates, "2026-05-16") is None


def test_build_theme_comparison_rows_classifies_changes():
    current = [
        {"key": "rates", "name": "米金利", "score": 5.0, "status": "主テーマ候補"},
        {"key": "energy", "name": "原油", "score": 2.0, "status": "監視候補"},
        {"key": "semis", "name": "半導体", "score": 3.0, "status": "浮上中"},
    ]
    previous = [
        {"key": "rates", "name": "米金利", "score": 3.5, "status": "浮上中"},
        {"key": "energy", "name": "原油", "score": 4.0, "status": "主テーマ候補"},
        {"key": "geopolitics", "name": "地政学", "score": 3.0, "status": "監視候補"},
    ]

    rows = build_theme_comparison_rows(current, previous)
    by_key = {row["key"]: row for row in rows}

    assert by_key["semis"]["state"] == "新規"
    assert by_key["rates"]["state"] == "強化"
    assert by_key["energy"]["state"] == "弱体化"
    assert by_key["geopolitics"]["state"] == "消滅"
    assert by_key["rates"]["score_change"] == 1.5


def test_build_theme_history_rows_flattens_snapshots_by_date():
    rows = build_theme_history_rows({
        "2026-05-18": [
            {
                "key": "rates",
                "name": "米金利",
                "score": "4.5",
                "status": "主テーマ候補",
                "confidence": "medium",
                "evidence": ["米10年"],
                "related_sectors": ["銀行業", "不動産業"],
                "unverified_data": ["ドル円"],
            }
        ]
    })

    assert rows == [{
        "date": "2026-05-18",
        "key": "rates",
        "name": "米金利",
        "score": 4.5,
        "status": "主テーマ候補",
        "confidence": "medium",
        "evidence_count": 1,
        "related_sectors": "銀行業, 不動産業",
        "unverified_count": 1,
    }]


def test_build_theme_transition_prompt_block_renders_change_instruction():
    block = build_theme_transition_prompt_block(
        target_date="2026-05-18",
        previous_date="2026-05-17",
        current_source="saved_snapshot",
        current_themes=[
            {
                "key": "rates",
                "name": "米金利",
                "score": 5.0,
                "status": "主テーマ候補",
                "related_sectors": ["銀行業"],
            }
        ],
        previous_themes=[
            {
                "key": "rates",
                "name": "米金利",
                "score": 3.0,
                "status": "監視候補",
                "related_sectors": ["銀行業"],
            }
        ],
    )

    assert "【市場テーマ履歴比較】" in block
    assert "[強化] 米金利" in block
    assert "theme_shift_analysis" in block
    assert "実測値ではない" in block
