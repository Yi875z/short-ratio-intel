from src.macro_context.news_fetcher import (
    TavilyNewsFetcher,
    build_market_news_queries,
    render_news_items_for_prompt,
)


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "results": [
                {
                    "title": "Japan stocks fall as rates rise",
                    "url": "https://www.reuters.com/markets/test",
                    "content": "Nikkei fell as US yields and yen moves weighed.",
                    "published_date": "2026-05-18",
                    "score": 0.91,
                }
            ]
        }


def test_build_market_news_queries_contains_target_date():
    queries = build_market_news_queries("2026-05-18")

    assert queries
    assert any("2026年05月18日" in query for query in queries)


def test_tavily_news_fetcher_parses_results(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _FakeResponse()

    monkeypatch.setattr("src.macro_context.news_fetcher.requests.post", fake_post)

    fetcher = TavilyNewsFetcher(api_key="test-key", max_results=1, timeout_seconds=3)
    items = fetcher.fetch("test query")

    assert len(items) == 1
    assert items[0].source == "reuters.com"
    assert items[0].query == "test query"
    assert calls[0][1]["api_key"] == "test-key"


def test_render_news_items_for_prompt_handles_empty():
    assert "ニュース検索結果なし" in render_news_items_for_prompt([])
