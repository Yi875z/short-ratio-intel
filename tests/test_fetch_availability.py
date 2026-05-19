from app import streamlit_app


class _NoFallbackClient:
    def get_short_ratio_by_date(self, target_date):
        return []

    def get_market_short_ratio_by_date(self, target_date):
        return None


def test_fetch_availability_detects_jpx_available(monkeypatch):
    class FakeJPX:
        def get_sector_breakdown_by_date(self, target_date):
            return [{"Date": target_date, "S33": "0050"}] * 34

        def get_market_breakdown_by_date(self, target_date):
            return {"Date": target_date, "ShortRatioPct": 43.0}

    monkeypatch.setattr(streamlit_app, "JPXShortSellingClient", FakeJPX)
    monkeypatch.setattr(streamlit_app, "JQuantsClient", _NoFallbackClient)

    result = streamlit_app.check_short_ratio_source_availability(
        "2026-05-19",
        saved_dates=[],
    )

    assert result["status"] == "取得可能"
    assert result["can_fetch"] is True
    assert result["partial"] is False
    assert result["sector_count"] == 34
    assert result["market_available"] is True
    assert result["sector_source"] == "jpx_pdf"
    assert result["market_source"] == "jpx_pdf"


def test_fetch_availability_uses_fallback_when_jpx_missing(monkeypatch):
    class FakeJPX:
        def get_sector_breakdown_by_date(self, target_date):
            return []

        def get_market_breakdown_by_date(self, target_date):
            return None

    class FakeFallback:
        def get_short_ratio_by_date(self, target_date):
            return [{"Date": target_date, "S33": "0050"}] * 33

        def get_market_short_ratio_by_date(self, target_date):
            return {"Date": target_date, "ShortRatioPct": 39.5}

    monkeypatch.setattr(streamlit_app, "JPXShortSellingClient", FakeJPX)
    monkeypatch.setattr(streamlit_app, "JQuantsClient", FakeFallback)

    result = streamlit_app.check_short_ratio_source_availability(
        "2026-05-15",
        saved_dates=[],
    )

    assert result["status"] == "取得可能"
    assert result["sector_source"] == "stock-marketdata"
    assert result["market_source"] == "stock-marketdata"


def test_fetch_availability_reports_partial_when_only_sector_exists(monkeypatch):
    class FakeJPX:
        def get_sector_breakdown_by_date(self, target_date):
            return [{"Date": target_date, "S33": "0050"}] * 34

        def get_market_breakdown_by_date(self, target_date):
            return None

    monkeypatch.setattr(streamlit_app, "JPXShortSellingClient", FakeJPX)
    monkeypatch.setattr(streamlit_app, "JQuantsClient", _NoFallbackClient)

    result = streamlit_app.check_short_ratio_source_availability(
        "2026-05-19",
        saved_dates=[],
    )

    assert result["status"] == "一部取得可能"
    assert result["can_fetch"] is False
    assert result["partial"] is True
    assert result["sector_source"] == "jpx_pdf"
    assert result["market_source"] == "none"


def test_fetch_availability_reports_unavailable_without_writing(monkeypatch):
    class FakeJPX:
        def get_sector_breakdown_by_date(self, target_date):
            return []

        def get_market_breakdown_by_date(self, target_date):
            return None

    monkeypatch.setattr(streamlit_app, "JPXShortSellingClient", FakeJPX)
    monkeypatch.setattr(streamlit_app, "JQuantsClient", _NoFallbackClient)

    result = streamlit_app.check_short_ratio_source_availability(
        "2026-05-20",
        saved_dates=["2026-05-18"],
    )

    assert result["status"] == "未公開または取得不可"
    assert result["can_fetch"] is False
    assert result["partial"] is False
    assert result["sector_source"] == "none"
    assert result["market_source"] == "none"
