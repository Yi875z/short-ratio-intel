"""RSSニュース取得モジュールの純粋関数テスト（ネットワーク非依存）。"""
from datetime import date

from src.macro_context.news_fetcher import MarketNewsItem
from src.macro_context.rss_news_fetcher import (
    _balance_by_label,
    _date_from_text,
    _in_window,
    _is_low_value_title,
)


def test_low_value_title_drops_ticker_slugs():
    # Google News の「 - 媒体」付きクオートスラッグは除外
    assert _is_low_value_title("6986.JSD - Reuters") is True
    assert _is_low_value_title("BFITa.BS - Reuters") is True
    assert _is_low_value_title("2204W.TW%5EB04 - Reuters") is True


def test_low_value_title_keeps_real_headlines():
    assert _is_low_value_title("米イラン、停戦60日延長 - Reuters") is False
    assert _is_low_value_title("Dell Soars Most Since 2018 on AI - Bloomberg") is False
    assert _is_low_value_title("日経平均1636円高") is False


def test_in_window_uses_jst_dates():
    after, before = date(2026, 5, 25), date(2026, 5, 29)  # [after, before)
    assert _in_window("2026-05-28 15:30", after, before) is True
    assert _in_window("2026-05-29 08:00", after, before) is False  # 翌営業日は除外
    assert _in_window("2026-05-24 12:00", after, before) is False  # 窓より前
    # 日付不明は安全側で採用
    assert _in_window("", after, before) is True


def test_date_from_text_extracts_iso_date():
    assert _date_from_text("2026-05-28 15:30") == date(2026, 5, 28)
    assert _date_from_text("no date here") is None


def test_balance_by_label_caps_per_source():
    def item(label):
        return MarketNewsItem(title="t", url="", snippet="", source=f"{label}/google")

    items = [item("ロイター")] * 6 + [item("日本経済新聞")] * 2
    balanced = _balance_by_label(items, max_per_label=3)
    labels = [it.source.split("/")[0] for it in balanced]
    assert labels.count("ロイター") == 3
    assert labels.count("日本経済新聞") == 2
