from src.macro_context.theme_detector import (
    build_market_theme_context,
    detect_market_themes,
)


def _summary():
    return {
        "sector_data": [
            {
                "sector_name": "空運業",
                "short_ratio_pct": 61.0,
                "dod_change": 4.2,
            },
            {
                "sector_name": "陸運業",
                "short_ratio_pct": 57.2,
                "dod_change": 1.1,
            },
            {
                "sector_name": "電気機器",
                "short_ratio_pct": 42.0,
                "dod_change": 0.2,
            },
        ]
    }


def test_detect_market_themes_prefers_energy_geopolitics_when_news_matches():
    themes = detect_market_themes(
        target_date="2026-05-18",
        today_summary=_summary(),
        extra_news="イランとイスラエル情勢、ホルムズ海峡リスク、WTI原油高止まり。",
        baseline_context="",
    )

    assert themes
    assert themes[0].key == "middle_east_energy"
    assert themes[0].score >= 3
    assert "空運業" in themes[0].related_sectors


def test_build_market_theme_context_marks_unverified_data():
    context = build_market_theme_context(
        target_date="2026-05-18",
        today_summary=_summary(),
        extra_news="米金利上昇とFedタカ派警戒。",
        baseline_context="",
    )

    assert "市場テーマ判定" in context
    assert "未確認データ" in context
    assert "事実として断定しない" in context
