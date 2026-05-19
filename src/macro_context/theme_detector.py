"""
市場が見ている主要テーマを空売り比率レポート用に整理する。

最小実装では、手動メモ・追加ニュース・固定ベースライン・当日の業種別
空売りデータを使ってテーマ候補を採点する。外部ニュースAPIは後続フェーズで
このモジュールへ接続する。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import MARKET_THEME_MAX_ITEMS, MARKET_THEME_MIN_SCORE
from src.macro_context.market_snapshot import build_market_snapshot


@dataclass(frozen=True)
class ThemeDefinition:
    key: str
    name: str
    keywords: list[str]
    market_channels: list[str]
    related_sectors: list[str]
    unverified_data: list[str]


@dataclass
class ThemeCandidate:
    key: str
    name: str
    score: float
    status: str
    confidence: str
    market_channels: list[str] = field(default_factory=list)
    related_sectors: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    short_ratio_alignment: str = "未判定"
    unverified_data: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "score": round(self.score, 2),
            "status": self.status,
            "confidence": self.confidence,
            "market_channels": self.market_channels,
            "related_sectors": self.related_sectors,
            "evidence": self.evidence,
            "short_ratio_alignment": self.short_ratio_alignment,
            "unverified_data": self.unverified_data,
        }


THEME_DEFINITIONS = [
    ThemeDefinition(
        key="middle_east_energy",
        name="中東地政学・原油供給リスク",
        keywords=[
            "イラン", "イスラエル", "米国", "ホルムズ", "中東",
            "原油", "WTI", "ブレント", "地政学", "停戦",
        ],
        market_channels=[
            "原油価格",
            "インフレ期待",
            "米金利",
            "日本株リスクオフ",
        ],
        related_sectors=[
            "空運業", "陸運業", "海運業", "輸送用機器",
            "ゴム製品", "電気・ガス業", "石油・石炭製品",
        ],
        unverified_data=["WTI", "ブレント", "米10年金利", "ドル円"],
    ),
    ThemeDefinition(
        key="us_rates_fed",
        name="米金利・Fed政策レジーム",
        keywords=[
            "米10年", "米2年", "金利", "利回り", "Fed", "FRB",
            "FOMC", "CPI", "PCE", "雇用統計", "インフレ",
        ],
        market_channels=[
            "米金利",
            "ドル円",
            "Nasdaq/SOX",
            "日本グロース株",
        ],
        related_sectors=[
            "電気機器", "精密機器", "情報・通信業", "不動産業",
            "銀行業", "保険業",
        ],
        unverified_data=["米10年金利", "米2年金利", "Fedイベント", "ドル円"],
    ),
    ThemeDefinition(
        key="boj_fx",
        name="BOJ・ドル円・日本株バリュー/グロース",
        keywords=[
            "BOJ", "日銀", "植田", "利上げ", "国債買い入れ",
            "ドル円", "USD/JPY", "円高", "円安",
        ],
        market_channels=[
            "ドル円",
            "国内金利",
            "銀行株",
            "輸出株",
        ],
        related_sectors=[
            "銀行業", "保険業", "輸送用機器", "電気機器",
            "不動産業", "その他金融業",
        ],
        unverified_data=["ドル円", "JGB利回り", "BOJ発言", "銀行株指数"],
    ),
    ThemeDefinition(
        key="us_tech_sox",
        name="米ハイテク・SOX・半導体連動",
        keywords=[
            "Nasdaq", "S&P500", "SOX", "半導体", "AI", "NVIDIA",
            "AMD", "TSMC", "米国株", "決算",
        ],
        market_channels=[
            "米ハイテク株",
            "SOX",
            "日本半導体関連",
            "グロース需給",
        ],
        related_sectors=[
            "電気機器", "精密機器", "機械", "化学", "情報・通信業",
        ],
        unverified_data=["Nasdaq", "SOX", "主要AI半導体株", "米決算"],
    ),
    ThemeDefinition(
        key="jpx_flows",
        name="JPX需給・投資主体別・先物手口",
        keywords=[
            "投資主体別", "海外勢", "信託銀行", "GPIF", "先物手口",
            "J-NET", "裁定", "信用残", "空売り比率",
        ],
        market_channels=[
            "海外勢先物",
            "信託銀行フロー",
            "裁定/J-NET",
            "信用需給",
        ],
        related_sectors=[
            "銀行業", "証券、商品先物取引業", "電気機器", "輸送用機器",
            "情報・通信業",
        ],
        unverified_data=["投資主体別売買動向", "先物手口", "J-NET", "信用残"],
    ),
    ThemeDefinition(
        key="options_vol",
        name="オプション・GEX・ボラティリティ",
        keywords=[
            "GEX", "Gamma", "ガンマ", "VIX", "日経VI", "IV",
            "SQ", "建玉", "Pinning", "スキュー", "ボラティリティ",
        ],
        market_channels=[
            "ディーラーガンマ",
            "先物ヘッジ",
            "IV/日経VI",
            "SQ需給",
        ],
        related_sectors=[
            "電気機器", "情報・通信業", "銀行業", "輸送用機器",
        ],
        unverified_data=["日経VI", "VIX", "オプション建玉", "ゼロガンマ水準"],
    ),
]


def detect_market_themes(
    target_date: str,
    today_summary: dict,
    extra_news: str = "",
    baseline_context: str = "",
    max_items: int = MARKET_THEME_MAX_ITEMS,
    min_score: float = MARKET_THEME_MIN_SCORE,
) -> list[ThemeCandidate]:
    """入力テキストと空売りデータから主要テーマ候補を返す。"""
    text = "\n".join([baseline_context or "", extra_news or ""])
    candidates: list[ThemeCandidate] = []

    for definition in THEME_DEFINITIONS:
        keyword_hits = _keyword_hits(text, definition.keywords)
        news_score = min(len(keyword_hits), 3)
        market_score = _market_reaction_score(text, definition.keywords)
        alignment_score, alignment_text, alignment_evidence = _short_ratio_alignment(
            today_summary, definition.related_sectors
        )
        score = news_score + market_score + alignment_score

        if score < min_score:
            continue

        evidence = []
        if keyword_hits:
            evidence.append(f"入力文脈の検出語: {', '.join(keyword_hits[:6])}")
        evidence.extend(alignment_evidence)
        if not evidence:
            evidence.append("入力データから明確な根拠は限定的。")

        candidates.append(
            ThemeCandidate(
                key=definition.key,
                name=definition.name,
                score=score,
                status=_status_from_score(score, keyword_hits),
                confidence=_confidence_from_score(score),
                market_channels=definition.market_channels,
                related_sectors=definition.related_sectors,
                evidence=evidence[:4],
                short_ratio_alignment=alignment_text,
                unverified_data=definition.unverified_data,
            )
        )

    if not candidates:
        return [
            ThemeCandidate(
                key="unclassified",
                name="主要テーマ未判定",
                score=0.0,
                status="要調査",
                confidence="low",
                evidence=[
                    "ニュース・市場データが不足しており、主要テーマを自動判定できない。"
                ],
                short_ratio_alignment="空売り比率との整合性は未判定。",
                unverified_data=[
                    "米金利", "ドル円", "VIX", "WTI", "SOX",
                    "日経先物", "投資主体別売買動向",
                ],
            )
        ]

    return sorted(candidates, key=lambda item: item.score, reverse=True)[:max_items]


def build_market_theme_context(
    target_date: str,
    today_summary: dict,
    extra_news: str = "",
    baseline_context: str = "",
) -> str:
    """Geminiへ渡す市場テーマコンテキストを構築する。"""
    source_text = "\n".join([baseline_context or "", extra_news or ""])
    snapshot = build_market_snapshot(target_date, source_text)
    themes = detect_market_themes(
        target_date=target_date,
        today_summary=today_summary,
        extra_news=extra_news,
        baseline_context=baseline_context,
    )

    lines = [
        "【市場テーマ判定】",
        snapshot.to_prompt_block(),
        "",
        "【主要テーマ候補】",
    ]

    for index, theme in enumerate(themes, 1):
        lines.extend([
            f"{index}. {theme.name}",
            f"   - 状態: {theme.status}",
            f"   - 重要度スコア: {theme.score:.1f}",
            f"   - 信頼度: {theme.confidence}",
            f"   - 影響経路: {', '.join(theme.market_channels)}",
            f"   - 関連業種: {', '.join(theme.related_sectors) if theme.related_sectors else '未特定'}",
            f"   - 空売り比率との整合性: {theme.short_ratio_alignment}",
            f"   - 根拠: {' / '.join(theme.evidence)}",
            f"   - 未確認データ: {', '.join(theme.unverified_data)}",
        ])

    lines.extend([
        "",
        "【テーマ利用ルール】",
        "- 上記テーマは入力データからの機械判定であり、未確認データは事実として断定しない。",
        "- テーマと業種別空売り比率が整合しない場合は、整合しないこと自体を明記する。",
        "- 主要テーマが変化している可能性がある場合は、テーマ転換シグナルとして条件付きで記述する。",
    ])
    return "\n".join(lines)


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _market_reaction_score(text: str, keywords: list[str]) -> float:
    if not any(keyword in text for keyword in keywords):
        return 0.0
    reaction_words = ["上昇", "下落", "急騰", "急落", "高止まり", "低下", "反発", "悪化"]
    return 1.0 if any(word in text for word in reaction_words) else 0.0


def _short_ratio_alignment(
    today_summary: dict,
    related_sectors: list[str],
) -> tuple[float, str, list[str]]:
    sector_data = today_summary.get("sector_data", []) or []
    matched = [
        sector
        for sector in sector_data
        if sector.get("sector_name") in set(related_sectors)
    ]
    if not matched:
        return 0.0, "関連業種データなし。", []

    high = [
        sector
        for sector in matched
        if (sector.get("short_ratio_pct") or 0) >= 47.0
    ]
    spikes = [
        sector
        for sector in matched
        if sector.get("dod_change") is not None
        and abs(sector.get("dod_change") or 0) >= 3.0
    ]

    evidence = []
    if high:
        evidence.append(
            "高空売り関連業種: "
            + ", ".join(
                f"{s['sector_name']}({s['short_ratio_pct']:.1f}%)"
                for s in high[:4]
            )
        )
    if spikes:
        evidence.append(
            "前日比急変関連業種: "
            + ", ".join(
                f"{s['sector_name']}({s['dod_change']:+.1f}pt)"
                for s in spikes[:4]
            )
        )

    score = min(len(high), 2) + min(len(spikes), 2) * 0.5
    if score >= 2.0:
        text = "高い。関連業種の空売り比率または前日比急変が目立つ。"
    elif score > 0:
        text = "一部あり。関連業種に高水準または急変がある。"
    else:
        text = "限定的。関連業種の空売り比率だけではテーマを裏付けにくい。"

    return score, text, evidence


def _status_from_score(score: float, keyword_hits: list[str]) -> str:
    if score >= 5.0 and keyword_hits:
        return "主テーマ候補"
    if score >= 3.0:
        return "浮上中"
    return "監視候補"


def _confidence_from_score(score: float) -> str:
    if score >= 5.0:
        return "high"
    if score >= 3.0:
        return "medium"
    return "low"
