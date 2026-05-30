"""
市場ニュース・手動メモ・固定ベースラインからAIレポート用コンテキストを作る。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import (
    CURRENT_MACRO_CONTEXT,
    MARKET_NEWS_AUTO_FETCH,
    MARKET_NEWS_RSS_ENABLED,
)
from src.macro_context.news_fetcher import (
    MarketNewsItem,
    TavilyNewsFetcher,
    build_market_news_queries,
)
from src.macro_context.rss_news_fetcher import RssNewsFetcher, render_rss_items_for_prompt
from src.macro_context.theme_detector import build_market_theme_context, detect_market_themes


@dataclass
class MarketContextBundle:
    target_date: str
    baseline_context: str
    manual_news: str = ""
    fetched_news: list[MarketNewsItem] = field(default_factory=list)
    market_theme_context: str = ""
    source_mode: str = "manual"

    @property
    def fetched_news_text(self) -> str:
        return render_rss_items_for_prompt(self.fetched_news)

    @property
    def combined_news_text(self) -> str:
        parts = []
        if self.manual_news:
            parts.append("【手動追加ニュース/市場メモ】\n" + self.manual_news)
        if self.fetched_news:
            parts.append("【ニュース検索結果】\n" + self.fetched_news_text)
        return "\n\n".join(parts)

    def to_prompt_block(self) -> str:
        lines = [
            "【市場コンテキスト収集モード】",
            f"- mode: {self.source_mode}",
            "- 注意: ニュース検索結果はテーマ候補の材料であり、未確認データは断定しない。",
            "",
            self.market_theme_context,
        ]

        if self.manual_news:
            lines.extend(["", "【手動追加ニュース/市場メモ】", self.manual_news])
        if self.fetched_news:
            lines.extend([
                "",
                f"【ニュース見出し（対象日スコープ / source={self.source_mode}）】",
                "- ロイター/日経/Bloomberg/Google News の見出しを対象営業日の窓に絞って取得。",
                "- 見出しは確定事実ではなく材料。数値は本文未確認のため断定しない。",
                self.fetched_news_text,
            ])
        else:
            lines.extend([
                "",
                "【ニュース見出し】",
                "自動ニュース取得なし。手動メモまたは固定ベースラインを使用。",
            ])

        return "\n".join(lines)


def build_market_context_bundle(
    target_date: str,
    today_summary: dict,
    manual_news: str = "",
    baseline_context: str = CURRENT_MACRO_CONTEXT,
    auto_fetch_news: bool = MARKET_NEWS_AUTO_FETCH,
    news_fetcher: TavilyNewsFetcher | None = None,
    rss_enabled: bool = MARKET_NEWS_RSS_ENABLED,
    rss_fetcher: RssNewsFetcher | None = None,
) -> MarketContextBundle:
    """
    Geminiへ渡す市場コンテキストを構築する。

    ニュースは2系統:
    - RSS（ロイター/日経/Bloomberg/Google News）: 無料・APIキー不要のため既定ON。
      対象営業日の窓に絞って取得するので過去日レポートでも日付整合が取れる。
    - Tavily: 要APIキーの任意の補助。auto_fetch_news=True かつキー設定時のみ併用。
    """
    fetched_news: list[MarketNewsItem] = []
    modes: list[str] = []

    if rss_enabled:
        fetcher = rss_fetcher or RssNewsFetcher()
        rss_result = fetcher.fetch_for_date(target_date)
        fetched_news.extend(rss_result.items)
        modes.append("rss" if rss_result.items else "rss_no_results")

    if auto_fetch_news:
        fetcher = news_fetcher or TavilyNewsFetcher()
        tavily_items = fetcher.fetch_many(build_market_news_queries(target_date))
        fetched_news.extend(tavily_items)
        modes.append("tavily" if tavily_items else "tavily_no_results")

    source_mode = "+".join(modes) if modes else "manual"

    combined_news = _combine_news_text(manual_news, fetched_news)
    market_theme_context = build_market_theme_context(
        target_date=target_date,
        today_summary=today_summary,
        extra_news=combined_news,
        baseline_context=baseline_context,
    )

    return MarketContextBundle(
        target_date=target_date,
        baseline_context=baseline_context,
        manual_news=manual_news,
        fetched_news=fetched_news,
        market_theme_context=market_theme_context,
        source_mode=source_mode,
    )


def build_theme_snapshot_dicts(
    target_date: str,
    today_summary: dict,
    manual_news: str = "",
    baseline_context: str = CURRENT_MACRO_CONTEXT,
) -> list[dict]:
    """保存用の市場テーマ判定辞書を作る。"""
    themes = detect_market_themes(
        target_date=target_date,
        today_summary=today_summary,
        extra_news=manual_news,
        baseline_context=baseline_context,
    )
    return [theme.to_dict() for theme in themes]


def _combine_news_text(manual_news: str, fetched_news: list[MarketNewsItem]) -> str:
    parts = []
    if manual_news:
        parts.append(manual_news)
    if fetched_news:
        parts.append(render_rss_items_for_prompt(fetched_news))
    return "\n\n".join(parts)
