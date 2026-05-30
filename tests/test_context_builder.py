from src.macro_context.context_builder import build_market_context_bundle
from src.macro_context.news_fetcher import MarketNewsItem
from src.macro_context.rss_news_fetcher import RssFeedHealth, RssFetchResult


class _FakeFetcher:
    """Tavily 互換のフェイク（fetch_many）。"""

    def fetch_many(self, queries):
        return [
            MarketNewsItem(
                title="US yields lift, yen weakens",
                url="https://example.com/rates",
                snippet="米10年金利上昇とドル円の変動が日本株の重し。",
                source="example.com",
                query=list(queries)[0],
                score=0.8,
            )
        ]


class _FakeRssFetcher:
    """RssNewsFetcher 互換のフェイク（fetch_for_date）。ネットワークを使わない。"""

    def fetch_for_date(self, target_date):
        return RssFetchResult(
            items=[
                MarketNewsItem(
                    title="日経平均は反発、米イラン停戦延長が追い風 - Reuters",
                    url="https://example.com/nikkei",
                    snippet="",
                    source="ロイター/google",
                    published_date=f"{target_date} 15:30",
                    query="gnews_reuters",
                    score=0.9,
                )
            ],
            health=[RssFeedHealth("gnews_reuters", "ロイター", "ok", 1, f"{target_date} 15:30")],
        )


def _summary():
    return {
        "sector_data": [
            {
                "sector_name": "電気機器",
                "short_ratio_pct": 48.5,
                "dod_change": 3.4,
            }
        ]
    }


def test_context_builder_manual_mode_when_all_sources_off():
    bundle = build_market_context_bundle(
        target_date="2026-05-18",
        today_summary=_summary(),
        manual_news="SOX下落、半導体株に売り。",
        baseline_context="",
        auto_fetch_news=False,
        rss_enabled=False,
    )

    block = bundle.to_prompt_block()
    assert bundle.source_mode == "manual"
    assert "手動追加ニュース" in block
    assert "市場テーマ判定" in block


def test_context_builder_uses_rss_by_default():
    bundle = build_market_context_bundle(
        target_date="2026-05-18",
        today_summary=_summary(),
        baseline_context="",
        auto_fetch_news=False,
        rss_enabled=True,
        rss_fetcher=_FakeRssFetcher(),
    )

    assert bundle.source_mode == "rss"
    assert bundle.fetched_news
    assert "ニュース見出し" in bundle.to_prompt_block()


def test_context_builder_adds_tavily_as_supplement():
    bundle = build_market_context_bundle(
        target_date="2026-05-18",
        today_summary=_summary(),
        baseline_context="",
        auto_fetch_news=True,
        news_fetcher=_FakeFetcher(),
        rss_enabled=True,
        rss_fetcher=_FakeRssFetcher(),
    )

    assert bundle.source_mode == "rss+tavily"
    assert len(bundle.fetched_news) == 2
