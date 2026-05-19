from src.macro_context.context_builder import build_market_context_bundle
from src.macro_context.news_fetcher import MarketNewsItem


class _FakeFetcher:
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


def test_context_builder_uses_manual_mode_by_default():
    bundle = build_market_context_bundle(
        target_date="2026-05-18",
        today_summary=_summary(),
        manual_news="SOX下落、半導体株に売り。",
        baseline_context="",
        auto_fetch_news=False,
    )

    block = bundle.to_prompt_block()
    assert bundle.source_mode == "manual"
    assert "手動追加ニュース" in block
    assert "市場テーマ判定" in block


def test_context_builder_auto_fetches_when_enabled():
    bundle = build_market_context_bundle(
        target_date="2026-05-18",
        today_summary=_summary(),
        baseline_context="",
        auto_fetch_news=True,
        news_fetcher=_FakeFetcher(),
    )

    assert bundle.source_mode == "auto_fetch"
    assert bundle.fetched_news
    assert "ニュース検索結果" in bundle.to_prompt_block()
