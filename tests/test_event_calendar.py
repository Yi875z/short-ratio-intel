"""市場イベント・カレンダーのテスト（計算ロジックは決定的なので外部依存なし）。"""
from datetime import date

from src.macro_context.event_calendar import (
    build_event_calendar_prompt_block,
    earnings_season_label,
    get_events_for_date,
    last_business_day,
    sq_date,
)


def test_sq_is_second_friday():
    # 2026年6月の第2金曜は6/12
    assert sq_date(2026, 6) == date(2026, 6, 12)
    # 2026年3月の第2金曜は3/13
    assert sq_date(2026, 3) == date(2026, 3, 13)


def test_msci_effective_is_last_business_day():
    # 2026-05-31は日曜、5-30は土曜 → 最終営業日は5/29
    assert last_business_day(2026, 5) == date(2026, 5, 29)


def test_msci_rebalance_surfaces_for_late_may():
    # 5/28レポートでは翌営業日(5/29)のMSCI半期リバランスを検出する
    events = get_events_for_date("2026-05-28")
    msci = [e for e in events if e.category == "msci"]
    assert msci, "MSCI入替イベントが検出されるべき"
    assert msci[0].event_date == date(2026, 5, 29)
    assert msci[0].importance == "high"


def test_june_has_major_sq_and_boj_fomc_week():
    events = get_events_for_date("2026-05-28")
    cats = {e.category for e in events}
    assert "sq" in cats and "boj" in cats and "fomc" in cats


def test_prompt_block_contains_interpretation_rule():
    block = build_event_calendar_prompt_block("2026-05-28")
    assert "MSCI" in block
    assert "機械的" in block  # 機械的フローと方向性売りを混同しない旨


def test_earnings_season_label_outside_window():
    # 5/28は本決算集中期(〜5/15)を過ぎている
    assert earnings_season_label(date(2026, 5, 28)) == ""
    # 5/8は本決算集中期の中
    assert earnings_season_label(date(2026, 5, 8)) != ""
