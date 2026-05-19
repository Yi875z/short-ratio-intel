"""
市場テーマ判定で使う日米マーケット確認項目。

現時点では外部APIに依存しない最小実装とし、ニュース本文や手動メモに
含まれる市場指標を抽出する。実データ取得は次フェーズで差し替える。
"""
from __future__ import annotations

from dataclasses import dataclass, field


INDICATOR_KEYWORDS = {
    "us_rates": ["米10年", "米2年", "金利", "利回り", "Fed", "FRB", "FOMC"],
    "fx": ["ドル円", "USD/JPY", "円高", "円安", "DXY"],
    "volatility": ["VIX", "日経VI", "ボラティリティ", "IV"],
    "commodities": ["WTI", "原油", "ブレント", "Gold", "金先物"],
    "us_equities": ["Nasdaq", "S&P500", "SOX", "半導体", "米国株"],
    "jp_equities": ["日経平均", "TOPIX", "日経先物", "グロース", "バリュー"],
    "jpx_flows": ["投資主体別", "先物手口", "J-NET", "裁定", "信用残"],
    "options": ["GEX", "Gamma", "SQ", "建玉", "Pinning", "スキュー"],
}


@dataclass(frozen=True)
class MarketSnapshot:
    """レポート作成時点で確認できた市場情報の薄いスナップショット。"""

    target_date: str
    observed_categories: list[str] = field(default_factory=list)
    observed_terms: list[str] = field(default_factory=list)
    missing_categories: list[str] = field(default_factory=list)
    source_note: str = "manual_or_news_text"

    def to_prompt_block(self) -> str:
        observed = ", ".join(self.observed_terms) if self.observed_terms else "なし"
        missing = ", ".join(self.missing_categories) if self.missing_categories else "なし"
        return (
            f"- 対象日: {self.target_date}\n"
            f"- 入力文中で確認できた市場指標: {observed}\n"
            f"- 未取得カテゴリ: {missing}\n"
            "- 注意: 未取得カテゴリの数値・方向性は事実として断定しない。"
        )


def build_market_snapshot(target_date: str, text: str = "") -> MarketSnapshot:
    """ニュース本文・手動メモから市場指標カテゴリを抽出する。"""
    observed_categories = []
    observed_terms = []

    for category, keywords in INDICATOR_KEYWORDS.items():
        matched = [keyword for keyword in keywords if keyword in text]
        if matched:
            observed_categories.append(category)
            observed_terms.extend(matched)

    missing_categories = [
        category
        for category in INDICATOR_KEYWORDS
        if category not in set(observed_categories)
    ]

    return MarketSnapshot(
        target_date=target_date,
        observed_categories=observed_categories,
        observed_terms=sorted(set(observed_terms)),
        missing_categories=missing_categories,
    )
