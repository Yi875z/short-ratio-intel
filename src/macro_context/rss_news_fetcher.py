"""
RSSベースの市場ニュース取得。

ロイター/日経/Bloomberg の見出しを無料RSSで集約する。設計方針:

- 一次ソース: 第三者RSS(wor.jp の Reuters/Nikkei) と公式RSS(Bloomberg 英語)。
- 背骨かつ自動フォールバック: Google News RSS。site: で媒体を絞り、
  after:/before: 演算子で「レポート対象日の窓」に絞って取得できる。
  第三者RSSは現在の見出ししか返さず過去日を遡れないため、過去日レポートでも
  Google News 側で対象日のニュースを取り直せるようにしている。
- 鮮度監視: フィードごとに取得状況・件数・最新記事日時を記録し、403や更新停止
  （例: Bloomberg日本語版の旧URL停止）を検知したら Google News に退避する。
- 著作権配慮: 既定では見出し・公開日時・URL・媒体・分類タグのみを扱い、本文は
  保存しない（MARKET_NEWS_RSS_INCLUDE_SUMMARY=True のときだけ短い要約を一時利用）。

出力は既存の MarketNewsItem に揃え、Tavily 経路と同じパイプラインへ載せる。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
from loguru import logger

try:  # feedparser は requirements に追加済み。未導入環境では取得をスキップする。
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore[assignment]

from config.settings import (
    MARKET_NEWS_RSS_INCLUDE_SUMMARY,
    MARKET_NEWS_RSS_MAX_ITEMS,
    MARKET_NEWS_RSS_RECENT_DAYS,
    MARKET_NEWS_RSS_WINDOW_DAYS,
    MARKET_NEWS_TIMEOUT_SECONDS,
)
from src.macro_context.news_fetcher import MarketNewsItem

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) short-ratio-intel/1.0"
)
_GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class RssFeed:
    """取得対象のRSSフィード定義。"""

    key: str
    label: str          # 媒体名（プロンプト表示用）: ロイター / 日本経済新聞 / Bloomberg
    category: str       # top / business / forex / markets / technology / news / google
    lang: str           # ja / en
    kind: str           # "static"（固定URL） | "google_news"（site:+日付スコープ）
    url: str = ""       # static 用の固定URL
    site: str = ""      # google_news 用の site: フィルタ
    query: str = ""     # google_news 用のトピック検索式（site の代わり）


# 既定のフィード構成。
# 第三者RSS(wor.jp)と公式RSS(Bloomberg)を一次に、Google Newsを日付スコープ可能な
# 背骨兼フォールバックに置く。Google News は常時取得するため、第三者RSSが403や
# 更新停止でも対象媒体のカバレッジが途切れない。
DEFAULT_FEEDS: list[RssFeed] = [
    # 注: wor.jp のロイター系(top/business/forex)は2026-05時点でchannelのみ・item空/403、
    # 日経 news.rdf は一般速報(スポーツ等)で市場関連性が低いため既定から除外。
    # 日本語の市場ニュースは Google News(site:+日付スコープ) を背骨にする。
    # --- 一次: 公式RSS(Bloomberg) 英語・市場特化 ---
    RssFeed("bloomberg_markets", "Bloomberg", "markets", "en", "static",
            url="https://feeds.bloomberg.com/markets/news.rss"),
    RssFeed("bloomberg_tech", "Bloomberg", "technology", "en", "static",
            url="https://feeds.bloomberg.com/technology/news.rss"),
    # --- 背骨/フォールバック: Google News RSS(媒体別・日付スコープ可) ---
    RssFeed("gnews_reuters", "ロイター", "google", "ja", "google_news",
            site="jp.reuters.com"),
    RssFeed("gnews_nikkei", "日本経済新聞", "google", "ja", "google_news",
            site="nikkei.com"),
    RssFeed("gnews_bloomberg", "Bloomberg", "google", "ja", "google_news",
            site="bloomberg.co.jp"),
    # --- 市況まとめ取りこぼし防止: 市場ドライバー語のトピック検索 ---
    RssFeed("gnews_market", "市場まとめ", "topic", "ja", "google_news",
            query="日経平均 OR TOPIX OR ドル円 OR 日銀 OR 米金利 OR FOMC OR 半導体 OR 原油"),
]


@dataclass
class RssFeedHealth:
    """1フィードの取得結果サマリー（鮮度監視用）。"""

    key: str
    label: str
    status: str          # ok / empty / no_entries / skipped_old_target / error:<Type>
    kept: int            # 窓内で採用した件数
    latest_date: str     # フィード内の最新記事日時（窓フィルタ前）

    def to_line(self) -> str:
        latest = f" 最新={self.latest_date}" if self.latest_date else ""
        return f"  [{self.status}] {self.key}({self.label}) 採用{self.kept}件{latest}"


@dataclass
class RssFetchResult:
    """RSS取得の結果（採用ニュース＋フィード健全性）。"""

    items: list[MarketNewsItem] = field(default_factory=list)
    health: list[RssFeedHealth] = field(default_factory=list)

    @property
    def stale_or_failed(self) -> list[RssFeedHealth]:
        bad = {"empty", "no_entries"}
        return [h for h in self.health if h.status.startswith("error") or h.status in bad]

    def render_health_for_prompt(self) -> str:
        if not self.health:
            return "  RSS健全性情報なし"
        return "\n".join(h.to_line() for h in self.health)


class RssNewsFetcher:
    """RSSフィード群から対象日の市場ニュース見出しを集約する。"""

    def __init__(
        self,
        feeds: list[RssFeed] | None = None,
        window_days: int = MARKET_NEWS_RSS_WINDOW_DAYS,
        recent_days: int = MARKET_NEWS_RSS_RECENT_DAYS,
        max_items: int = MARKET_NEWS_RSS_MAX_ITEMS,
        max_per_label: int = 5,
        timeout_seconds: int = MARKET_NEWS_TIMEOUT_SECONDS,
        include_summary: bool = MARKET_NEWS_RSS_INCLUDE_SUMMARY,
    ) -> None:
        self.feeds = feeds if feeds is not None else DEFAULT_FEEDS
        self.window_days = window_days
        self.recent_days = recent_days
        self.max_items = max_items
        self.max_per_label = max_per_label
        self.timeout_seconds = timeout_seconds
        self.include_summary = include_summary

    def fetch_for_date(self, target_date: str) -> RssFetchResult:
        """対象日の窓に絞ってニュースを取得する。

        対象日が直近(recent_days以内)なら第三者RSSも併用、過去日なら現在見出ししか
        返さない第三者RSSはスキップし Google News の日付スコープ取得だけを使う。
        """
        if feedparser is None:
            logger.warning("feedparser未導入のためRSSニュース取得をスキップします")
            return RssFetchResult()

        target = _parse_date(target_date)
        today = date.today()
        is_recent = target is None or (today - target).days <= self.recent_days
        after = (target - timedelta(days=self.window_days)) if target else None
        before = (target + timedelta(days=1)) if target else None  # before: は排他なので翌日

        items: list[MarketNewsItem] = []
        health: list[RssFeedHealth] = []
        seen: set[str] = set()

        for feed in self.feeds:
            if feed.kind == "static" and not is_recent:
                health.append(RssFeedHealth(feed.key, feed.label, "skipped_old_target", 0, ""))
                continue

            url = self._feed_url(feed, after, before)
            raw, err = self._fetch(url)
            if err:
                health.append(RssFeedHealth(feed.key, feed.label, f"error:{err}", 0, ""))
                continue

            parsed = feedparser.parse(raw)
            entries = list(parsed.entries or [])
            latest = ""
            kept = 0
            for entry in entries:
                item = self._to_item(entry, feed)
                if item is None:
                    continue
                if item.published_date:
                    latest = max(latest, item.published_date) if latest else item.published_date
                # Google News の after:/before: は境界が緩く別営業日が混じるため、
                # 公開日時の窓フィルタを全フィードに適用して対象日の正確性を担保する
                if not _in_window(item.published_date, after, before):
                    continue
                url_key = item.url or ""
                title_key = _norm_title(item.title)
                if url_key in seen or title_key in seen:
                    continue
                if url_key:
                    seen.add(url_key)
                seen.add(title_key)
                items.append(item)
                kept += 1

            if err:
                status = f"error:{err}"
            elif not entries:
                status = "no_entries"
            elif kept == 0:
                status = "empty"
            else:
                status = "ok"
            health.append(RssFeedHealth(feed.key, feed.label, status, kept, latest))

        items.sort(key=lambda it: (it.published_date, it.score), reverse=True)
        # 媒体偏重を防ぐため媒体ごとに上限を設けてから全体を切り詰める
        items = _balance_by_label(items, self.max_per_label)[: self.max_items]

        bad = [h for h in health if h.status.startswith("error") or h.status in {"empty", "no_entries"}]
        if bad:
            logger.info(
                "RSS鮮度警告: {} フィードが空/失敗。Google Newsで補完済み",
                ", ".join(f"{h.key}({h.status})" for h in bad),
            )
        logger.info("RSSニュース取得: {}件採用 / 対象日={}", len(items), target_date)
        return RssFetchResult(items=items, health=health)

    def _feed_url(self, feed: RssFeed, after: date | None, before: date | None) -> str:
        if feed.kind == "google_news":
            query = feed.query if feed.query else f"site:{feed.site}"
            if after and before:
                query += f" after:{after.isoformat()} before:{before.isoformat()}"
            return (
                f"{_GOOGLE_NEWS_BASE}?q={quote_plus(query)}"
                "&hl=ja&gl=JP&ceid=JP:ja"
            )
        return feed.url

    def _fetch(self, url: str) -> tuple[bytes, str]:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            return resp.content, ""
        except requests.RequestException as exc:
            logger.warning("RSS取得失敗 {}: {}", url, exc)
            return b"", type(exc).__name__

    def _to_item(self, entry, feed: RssFeed) -> MarketNewsItem | None:
        title = str(entry.get("title", "") or "").strip()
        link = str(entry.get("link", "") or "").strip()
        if not title or _is_low_value_title(title):
            return None
        published = _entry_date(entry)
        summary = ""
        if self.include_summary:
            summary = _clip(_strip_html(str(entry.get("summary", "") or "")), 140)
        return MarketNewsItem(
            title=title,
            url=link,
            snippet=summary,
            source=f"{feed.label}/{feed.category}",
            published_date=published,
            query=feed.key,
            score=_recency_score(published),
        )


def render_rss_items_for_prompt(items: list[MarketNewsItem], limit: int = 12) -> str:
    """取得ニュースをGemini入力用の短いテキストへ整形する（見出し中心）。"""
    if not items:
        return "RSSニュース取得結果なし。"
    lines = []
    for index, item in enumerate(items[:limit], 1):
        published = f" / {item.published_date}" if item.published_date else ""
        url = f" / url={item.url}" if item.url else ""
        snippet = f"\n   {item.snippet}" if item.snippet else ""
        lines.append(f"{index}. [{item.source}{published}] {item.title}{url}{snippet}")
    return "\n".join(lines)


def _balance_by_label(items: list[MarketNewsItem], max_per_label: int) -> list[MarketNewsItem]:
    """媒体（source の "媒体名/分類" の媒体名部分）ごとに件数上限をかける。"""
    if max_per_label <= 0:
        return items
    counts: dict[str, int] = {}
    balanced: list[MarketNewsItem] = []
    for item in items:
        label = item.source.split("/", 1)[0]
        if counts.get(label, 0) >= max_per_label:
            continue
        counts[label] = counts.get(label, 0) + 1
        balanced.append(item)
    return balanced


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _entry_date(entry) -> str:
    """feedparser の published_parsed / updated_parsed(UTC) を JST の ISO 文字列へ。

    日本市場レポートの「対象営業日」を正しく切り出すため、UTC を JST(+9h)へ
    変換してから日時を返す（窓フィルタも JST 日付で判定される）。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            utc_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            return utc_dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pass
    raw = str(entry.get("published", "") or entry.get("updated", "") or "")
    return raw.strip()


def _in_window(published_date: str, after: date | None, before: date | None) -> bool:
    """記事日時が [after, before) の窓に入るか。日付未取得は安全側で採用する。"""
    if after is None or before is None:
        return True
    d = _date_from_text(published_date)
    if d is None:
        return True
    return after <= d < before


def _date_from_text(text: str) -> date | None:
    if not text:
        return None
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _recency_score(published_date: str) -> float:
    d = _date_from_text(published_date)
    if d is None:
        return 0.0
    age = (date.today() - d).days
    return 1.0 / (1.0 + max(age, 0))


_JP_CHAR_RE = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


def _is_low_value_title(title: str) -> bool:
    """株価クオートページのスラッグ等（例: 6986.JSD, BFITa.BS）を除外する。

    実際の見出しは必ず空白を含む（英語）か日本語文字を含む。どちらも無く短い
    英数記号だけの文字列はティッカー/クオートのノイズとみなす。
    """
    # Google News が付ける末尾の「 - 媒体名」を除いた本体で判定する
    core = re.sub(r"\s*[-–—]\s*[^-–—]+$", "", title).strip()
    if " " in core or "　" in core:
        return False
    if _JP_CHAR_RE.search(core):
        return False
    return len(core) <= 24


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", "", title).lower()


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


def _clip(text: str, max_chars: int) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[:max_chars] + "…"
