"""
市場テーマ判定用のニュース取得。

外部APIは明示設定された場合だけ使う。通常運用では、手動メモや
Streamlit側から渡される追加ニュースだけでも動作する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import requests
from loguru import logger

from config.settings import (
    MARKET_NEWS_MAX_RESULTS,
    MARKET_NEWS_TIMEOUT_SECONDS,
    TAVILY_API_KEY,
)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class MarketNewsItem:
    title: str
    url: str
    snippet: str
    source: str
    published_date: str = ""
    query: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published_date": self.published_date,
            "query": self.query,
            "score": self.score,
        }


class NewsFetchError(RuntimeError):
    """ニュース取得に失敗した場合の例外。"""


class TavilyNewsFetcher:
    """Tavily Search APIを使う市場ニュース取得クライアント。"""

    def __init__(
        self,
        api_key: str = TAVILY_API_KEY,
        max_results: int = MARKET_NEWS_MAX_RESULTS,
        timeout_seconds: int = MARKET_NEWS_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.max_results = max_results
        self.timeout_seconds = timeout_seconds

    def fetch(self, query: str) -> list[MarketNewsItem]:
        if not self.api_key:
            logger.info("TAVILY_API_KEY未設定のためニュース取得をスキップします")
            return []

        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "topic": "news",
            "max_results": self.max_results,
            "include_answer": False,
            "include_raw_content": False,
        }

        try:
            response = requests.post(
                TAVILY_SEARCH_URL,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NewsFetchError(f"Tavilyニュース取得に失敗しました: {exc}") from exc

        data = response.json()
        return _parse_tavily_results(data, query)

    def fetch_many(self, queries: Iterable[str]) -> list[MarketNewsItem]:
        items: list[MarketNewsItem] = []
        seen_urls: set[str] = set()

        for query in queries:
            try:
                for item in self.fetch(query):
                    dedupe_key = item.url or f"{item.title}:{item.snippet}"
                    if dedupe_key in seen_urls:
                        continue
                    seen_urls.add(dedupe_key)
                    items.append(item)
            except NewsFetchError as exc:
                logger.warning(str(exc))

        return sorted(items, key=lambda item: item.score, reverse=True)


def build_market_news_queries(target_date: str) -> list[str]:
    """対象日の市場テーマ調査クエリを作る。"""
    date_label = _date_label(target_date)
    return [
        f"{date_label} 日経平均 原因 米金利 ドル円 VIX 原油",
        f"{date_label} 日本株 市場テーマ 空売り比率 先物",
        f"{date_label} US market drivers rates dollar oil VIX SOX Japan stocks",
        f"{date_label} Reuters Japan stocks market drivers yen rates oil",
    ]


def render_news_items_for_prompt(items: list[MarketNewsItem], limit: int = 8) -> str:
    """取得ニュースをGemini入力用の短いテキストへ整形する。"""
    if not items:
        return "ニュース検索結果なし。"

    lines = []
    for index, item in enumerate(items[:limit], 1):
        published = f" / published={item.published_date}" if item.published_date else ""
        url = f" / url={item.url}" if item.url else ""
        lines.append(
            f"{index}. {item.title} ({item.source}{published})\n"
            f"   query={item.query}{url}\n"
            f"   snippet={item.snippet}"
        )
    return "\n".join(lines)


def _parse_tavily_results(data: dict, query: str) -> list[MarketNewsItem]:
    results = data.get("results", []) or []
    items = []
    for result in results:
        url = str(result.get("url", "") or "")
        title = str(result.get("title", "") or "")
        snippet = str(result.get("content", "") or result.get("snippet", "") or "")
        if not title and not snippet:
            continue
        items.append(
            MarketNewsItem(
                title=title,
                url=url,
                snippet=snippet,
                source=_source_from_url(url),
                published_date=str(result.get("published_date", "") or ""),
                query=query,
                score=float(result.get("score", 0) or 0),
            )
        )
    return items


def _source_from_url(url: str) -> str:
    if not url:
        return "unknown"
    domain = url.split("//")[-1].split("/")[0]
    return domain.replace("www.", "")


def _date_label(target_date: str) -> str:
    try:
        return datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y年%m月%d日")
    except ValueError:
        return target_date
