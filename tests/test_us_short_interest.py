"""空売り残高（隔週）の取り込みと表示のテスト（ネットワーク非依存）。"""
import pandas as pd
import pytest

from src.data_fetcher.finra_short_interest_client import (
    build_record,
    days_since_settlement,
)
from src.report.us_daily_report import build_short_interest_view

# 実APIの列名に合わせたサンプル
SAMPLE_ROW = {
    "accountingYearMonthNumber": "20260715",
    "symbolCode": "NVDA",
    "issueName": "NVIDIA Corporation Common Stoc",
    "currentShortPositionQuantity": "324052767",
    "previousShortPositionQuantity": "310126785",
    "averageDailyVolumeQuantity": "131174356",
    "daysToCoverQuantity": "2.47",
    "changePercent": "4.49",
    "settlementDate": "2026-07-15",
}


def test_record_keeps_every_field_and_normalizes_types():
    record = build_record(SAMPLE_ROW)

    assert record["SettlementDate"] == "2026-07-15"
    assert record["Ticker"] == "NVDA"
    assert record["CurrentShortPosition"] == 324052767
    assert record["PreviousShortPosition"] == 310126785
    assert record["DaysToCover"] == pytest.approx(2.47)
    assert record["ChangePercent"] == pytest.approx(4.49)


def test_record_fills_missing_values_with_none():
    """欠測列があっても KeyError にせず None を入れる。"""
    record = build_record({"symbolCode": "XYZ", "settlementDate": "2026-07-15"})

    assert record["CurrentShortPosition"] is None
    assert record["DaysToCover"] is None
    assert set(record) == {
        "SettlementDate", "Ticker", "IssueName", "CurrentShortPosition",
        "PreviousShortPosition", "AverageDailyVolume", "DaysToCover",
        "ChangePercent", "Source",
    }


def test_record_handles_empty_and_malformed_numbers():
    record = build_record(dict(SAMPLE_ROW, currentShortPositionQuantity="", daysToCoverQuantity="N/A"))

    assert record["CurrentShortPosition"] is None
    assert record["DaysToCover"] is None


# ------------------------------------------------------------------
# 経過日数
# ------------------------------------------------------------------

def test_days_since_settlement_counts_calendar_days():
    assert days_since_settlement("2026-07-15", as_of="2026-08-10") == 26
    assert days_since_settlement("2026-07-15", as_of="2026-07-15") == 0


def test_days_since_settlement_returns_none_for_bad_input():
    assert days_since_settlement("", as_of="2026-08-10") is None
    assert days_since_settlement("2026/07/15", as_of="2026-08-10") is None


# ------------------------------------------------------------------
# レポート表示
# ------------------------------------------------------------------

def _si_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"settlement_date": "2026-06-30", "ticker": "NVDA", "current_short_position": 310126785.0,
         "previous_short_position": 299666309.0, "change_percent": 3.49, "days_to_cover": 1.99,
         "average_daily_volume": 155989510.0},
        {"settlement_date": "2026-07-15", "ticker": "NVDA", "current_short_position": 324052767.0,
         "previous_short_position": 310126785.0, "change_percent": 4.49, "days_to_cover": 2.47,
         "average_daily_volume": 131174356.0},
        {"settlement_date": "2026-07-15", "ticker": "MU", "current_short_position": 36211849.0,
         "previous_short_position": 31669690.0, "change_percent": 14.34, "days_to_cover": 1.0,
         "average_daily_volume": 36000000.0},
    ])


def test_view_uses_only_the_latest_settlement_date():
    """古い基準日の行を混ぜない。"""
    view = build_short_interest_view(_si_frame(), target_date="2026-08-10")

    assert view["settlement_date"] == "2026-07-15"
    assert {r["ticker"] for r in view["rows"]} == {"NVDA", "MU"}


def test_view_states_settlement_date_and_elapsed_days():
    """残高がいつ時点の数字かを必ず示す（鮮度を隠さない）。"""
    view = build_short_interest_view(_si_frame(), target_date="2026-08-10")

    assert view["days_elapsed"] == 26
    assert "2026-07-15" in view["note"]
    assert "26日前" in view["note"]
    assert "フロー" in view["note"]      # 日次データとの違いを明示している


def test_view_sorts_by_position_size_and_adds_japanese_name():
    view = build_short_interest_view(_si_frame(), target_date="2026-08-10")

    assert [r["ticker"] for r in view["rows"]] == ["NVDA", "MU"]
    assert view["rows"][0]["name_ja"] == "エヌビディア"


def test_view_handles_absent_data():
    empty = build_short_interest_view(None, target_date="2026-08-10")

    assert empty["settlement_date"] is None
    assert empty["rows"] == []
    assert build_short_interest_view(pd.DataFrame(), "2026-08-10")["rows"] == []
