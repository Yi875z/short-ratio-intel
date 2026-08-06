"""米国日足クライアント（Yahoo chart API）の解析ロジックテスト（ネットワーク非依存）。"""
import pytest

from config.us_universe import to_yahoo_symbol
from src.data_fetcher.us_price_client import build_price_record, parse_chart_payload

# 実レスポンスと同じ構造。timestamp は取引所の寄付き時刻(UTC秒)
SAMPLE_PAYLOAD = {
    "chart": {
        "result": [
            {
                "meta": {
                    "symbol": "NVDA",
                    "gmtoffset": -14400,
                    "exchangeTimezoneName": "America/New_York",
                },
                "timestamp": [1785763800, 1785850200, 1785936600],
                "indicators": {
                    "quote": [
                        {
                            "open": [205.0, 207.5, 212.0],
                            "high": [208.0, 213.0, 220.0],
                            "low": [203.5, 206.0, 211.0],
                            "close": [206.64, 211.94, 219.22],
                            "volume": [128406900, 134922000, 157555300],
                        }
                    ],
                    "adjclose": [{"adjclose": [206.64, 211.94, 219.22]}],
                },
            }
        ],
        "error": None,
    }
}


def test_timestamps_become_exchange_local_dates():
    """UTC秒を取引所ローカル日付へ換算する（前日にずれない）。"""
    records = parse_chart_payload(SAMPLE_PAYLOAD, "NVDA")

    assert [r["Date"] for r in records] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert records[-1]["Close"] == pytest.approx(219.22)
    assert records[-1]["MarketVolume"] == pytest.approx(157555300)


def test_missing_values_stay_none_without_interpolation():
    """未確定の足は None のまま。前日値のコピーや補間はしない。"""
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": -14400},
                    "timestamp": [1785936600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [None],
                                "high": [None],
                                "low": [None],
                                "close": [None],
                                "volume": [None],
                            }
                        ]
                    },
                }
            ]
        }
    }

    records = parse_chart_payload(payload, "NVDA")

    assert len(records) == 1
    assert records[0]["Close"] is None
    assert records[0]["AdjClose"] is None
    assert records[0]["MarketVolume"] is None


def test_error_payload_returns_empty_list():
    payload = {"chart": {"result": None, "error": {"code": "Not Found"}}}

    assert parse_chart_payload(payload, "NOPE") == []
    assert parse_chart_payload({}, "NOPE") == []
    assert parse_chart_payload(None, "NOPE") == []


def test_build_price_record_always_contains_every_key():
    record = build_price_record(date_iso="2026-08-05", ticker="NVDA")

    assert set(record) == {
        "Date", "Ticker", "Open", "High", "Low", "Close", "AdjClose", "MarketVolume",
    }
    assert record["Close"] is None


def test_class_share_symbol_is_converted_for_yahoo():
    """FINRA は BRK/B、Yahoo は BRK-B。表記差を吸収する。"""
    assert to_yahoo_symbol("BRK/B") == "BRK-B"
    assert to_yahoo_symbol("NVDA") == "NVDA"
