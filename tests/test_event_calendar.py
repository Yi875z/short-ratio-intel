"""市場イベント・カレンダーのテスト（計算ロジックは決定的なので外部依存なし）。"""
from datetime import date

from src.macro_context.event_calendar import (
    build_event_calendar_prompt_block,
    earnings_season_label,
    get_events_for_date,
    get_events_for_month,
    last_business_day,
    sq_date,
)


def test_sq_is_second_friday():
    # 2026年6月の第2金曜は6/12
    assert sq_date(2026, 6) == date(2026, 6, 12)
    # 2026年3月の第2金曜は3/13
    assert sq_date(2026, 3) == date(2026, 3, 13)


def test_last_business_day():
    # 2026-05-31は日曜、5-30は土曜 → 最終営業日は5/29
    assert last_business_day(2026, 5) == date(2026, 5, 29)


def test_msci_may_review_has_three_phases():
    # 2026年5月のMSCI半期レビュー: 発表5/12 → 実売買5/29 → 発効6/1（公式日程）
    may = get_events_for_month(2026, 5)
    msci = [e for e in may if "MSCI" in e.name]
    phases = {e.phase: e.event_date for e in msci}
    assert phases["announcement"] == date(2026, 5, 12)
    assert phases["passive_trade"] == date(2026, 5, 29)
    june = get_events_for_month(2026, 6)
    effective = [e for e in june if "MSCI" in e.name and e.phase == "effective"]
    assert effective and effective[0].event_date == date(2026, 6, 1)


def test_msci_rebalance_surfaces_for_late_may():
    # 5/28レポートでは翌営業日(5/29)のMSCIパッシブ実売買日を検出する
    events = get_events_for_date("2026-05-28")
    msci = [e for e in events if "MSCI" in e.name and e.phase == "passive_trade"]
    assert msci, "MSCI実売買イベントが検出されるべき"
    assert msci[0].event_date == date(2026, 5, 29)
    assert msci[0].importance == "high"


def test_june_has_major_sq_and_boj_fomc_week():
    events = get_events_for_date("2026-05-28")
    cats = {e.category for e in events}
    assert "sq" in cats and "boj" in cats and "fomc" in cats


def test_nfp_uses_official_bls_dates():
    # 2026年2月の雇用統計(1月分)は政府閉鎖で2/11(水)に変更された実績日
    feb = get_events_for_month(2026, 2)
    nfp = [e for e in feb if e.category == "nfp"]
    assert nfp and nfp[0].event_date == date(2026, 2, 11)
    # 12月分は第1金曜ではなく1/9（第2金曜）が公式日
    jan = get_events_for_month(2026, 1)
    nfp_jan = [e for e in jan if e.category == "nfp"]
    assert nfp_jan and nfp_jan[0].event_date == date(2026, 1, 9)


def test_us_cpi_covers_second_half_of_2026():
    # 旧版はH2未収録だった。7月分CPI=7/14を確認
    jul = get_events_for_month(2026, 7)
    cpi = [e for e in jul if e.category == "cpi" and e.region == "US"]
    assert cpi and cpi[0].event_date == date(2026, 7, 14)


def test_boj_december_meeting_present():
    dec = get_events_for_month(2026, 12)
    boj = [e for e in dec if e.category == "boj"]
    assert boj and boj[0].event_date == date(2026, 12, 18)


def test_dividend_ex_date_is_one_business_day_before_record():
    # 2026年3月: 権利確定日3/31(火) → 権利落ち日3/30(月)
    mar = get_events_for_month(2026, 3)
    div = [e for e in mar if e.category == "dividend"]
    assert div and div[0].event_date == date(2026, 3, 30)
    # 2026年9月: 権利確定日9/30(水) → 権利落ち日9/29(火)
    sep = get_events_for_month(2026, 9)
    div_sep = [e for e in sep if e.category == "dividend"]
    assert div_sep and div_sep[0].event_date == date(2026, 9, 29)


def test_nikkei225_spring_review_2026():
    # 2026年春: 発表3/5 → 実売買3/31 → 発効4/1（公式リリース）
    mar = get_events_for_month(2026, 3)
    n225 = {e.phase: e.event_date for e in mar if "日経平均" in e.name}
    assert n225["announcement"] == date(2026, 3, 5)
    assert n225["passive_trade"] == date(2026, 3, 31)
    apr = get_events_for_month(2026, 4)
    eff = [e for e in apr if "日経平均" in e.name and e.phase == "effective"]
    assert eff and eff[0].event_date == date(2026, 4, 1)


def test_topix_october_rebalance_and_transition():
    oct_events = get_events_for_month(2026, 10)
    topix = [e for e in oct_events if "TOPIX" in e.name]
    effective = [e for e in topix if e.phase == "effective"]
    # 定期リバランス + 移行措置1st段階の両方が10月末(10/30)に存在
    assert {e.event_date for e in effective} == {date(2026, 10, 30)}
    assert any("移行措置" in e.name for e in topix)
    # 実売買日は前営業日10/29
    passive = [e for e in topix if e.phase == "passive_trade"]
    assert all(e.event_date == date(2026, 10, 29) for e in passive)


def test_ftse_march_2026_holiday_adjustment():
    # 2026年3月の第3金曜(3/20)は春分の日 → 実売買は3/19
    mar = get_events_for_month(2026, 3)
    ftse = [e for e in mar if "FTSE" in e.name and e.phase == "passive_trade"]
    assert ftse and ftse[0].event_date == date(2026, 3, 19)


def test_ism_first_us_business_day():
    # 2026年1月: 1/1は連邦祝日 → ISM製造業は1/2
    jan = get_events_for_month(2026, 1)
    ism = [e for e in jan if e.category == "ism" and "製造業" in e.name]
    assert ism and ism[0].event_date == date(2026, 1, 2)


def test_tankan_quarterly_releases():
    jul = get_events_for_month(2026, 7)
    tankan = [e for e in jul if e.category == "tankan"]
    assert tankan and tankan[0].event_date == date(2026, 7, 1)


def test_prompt_block_contains_interpretation_rule_and_phases():
    block = build_event_calendar_prompt_block("2026-05-28")
    assert "MSCI" in block
    assert "機械的" in block  # 機械的フローと方向性売りを混同しない旨
    assert "実売買" in block  # フェーズラベルが入る
    # low重要度（GPIFウォッチ・企業物価目安）はプロンプトから除外
    assert "GPIF" not in block


def test_calendar_works_for_2027_fallback():
    # キュレーション外の年もルール計算のフォールバックで動く
    events = get_events_for_date("2027-03-10")
    assert any(e.category == "sq" for e in events)
    mar = get_events_for_month(2027, 3)
    assert any("FTSE" in e.name for e in mar)


def test_earnings_season_label_outside_window():
    # 5/28は本決算集中期(〜5/15)を過ぎている
    assert earnings_season_label(date(2026, 5, 28)) == ""
    # 5/8は本決算集中期の中
    assert earnings_season_label(date(2026, 5, 8)) != ""
