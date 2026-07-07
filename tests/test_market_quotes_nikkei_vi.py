"""日経VIの解析・配置ロジックの決定論テスト（ネットワーク非依存）。"""
from src.macro_context.market_quotes import (
    Quote,
    _insert_nikkei_vi,
    _parse_smd_number,
)


def test_parse_smd_number_handles_comma_percent_and_blank():
    assert _parse_smd_number("29.70") == 29.70
    assert _parse_smd_number("-20.50%") == -20.50
    assert _parse_smd_number("1,234.5") == 1234.5
    assert _parse_smd_number("") is None
    assert _parse_smd_number(None) is None
    assert _parse_smd_number("—") is None


def _q(label, ticker, category="金利・リスク"):
    return Quote(label=label, ticker=ticker, category=category, unit="", ok=True)


def test_insert_nikkei_vi_goes_right_after_vix():
    vi = _q("日経VI", "NKVI")
    daily = [
        _q("米10年金利", "811"),
        _q("VIX恐怖指数", "621"),
        _q("WTI原油", "921"),
    ]
    out = _insert_nikkei_vi(daily, vi)
    labels = [q.label for q in out]
    assert labels == ["米10年金利", "VIX恐怖指数", "日経VI", "WTI原油"]


def test_insert_nikkei_vi_appends_when_vix_absent():
    vi = _q("日経VI", "NKVI")
    daily = [_q("日経平均(現物)", "111", category="日本株")]
    out = _insert_nikkei_vi(daily, vi)
    assert out[-1].label == "日経VI"
    assert len(out) == 2
