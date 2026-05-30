"""運用者のハウスビュー（常設の相場観）の読込・鮮度判定・プロンプト整形。

固定の CURRENT_MACRO_CONTEXT に代わり、運用者が随時更新する「今の相場観」を
レポートの支配的マクロ背景の起点にする。平日19時の自動実行には人がいないため、
毎回入力ではなく Supabase に永続化した常設ビューを使い、古くなったら鮮度警告を出す。
レポートは AI 単独の判断ではなく、このハウスビューと当日データの突合として書かせる。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config.settings import CURRENT_MACRO_CONTEXT, HOUSE_VIEW_STALE_DAYS
from src.storage.db import get_house_view, save_house_view

_JST = timezone(timedelta(hours=9))


@dataclass
class HouseView:
    content: str
    updated_at: datetime | None = None  # UTC naive（DB保存値）

    @property
    def age_days(self) -> int | None:
        if self.updated_at is None:
            return None
        return (datetime.utcnow() - self.updated_at).days

    def is_stale(self, max_age_days: int = HOUSE_VIEW_STALE_DAYS) -> bool:
        age = self.age_days
        return age is not None and age > max_age_days

    def updated_label(self) -> str:
        if self.updated_at is None:
            return "更新日時不明"
        jst = self.updated_at.replace(tzinfo=timezone.utc).astimezone(_JST)
        return jst.strftime("%Y-%m-%d %H:%M JST")


def load_house_view() -> HouseView | None:
    """保存済みハウスビューを返す。無ければ None。"""
    record = get_house_view()
    if not record:
        return None
    content, updated_at = record
    return HouseView(content=content, updated_at=updated_at)


def store_house_view(content: str) -> None:
    """ハウスビューを保存する。"""
    save_house_view(content.strip())


def effective_macro_context() -> tuple[str, str]:
    """テーマ判定に使う支配的マクロ背景テキストと出所ラベルを返す。

    ハウスビューがあれば優先、無ければ固定の CURRENT_MACRO_CONTEXT にフォールバック。
    """
    hv = load_house_view()
    if hv and hv.content.strip():
        return hv.content, "house_view"
    return CURRENT_MACRO_CONTEXT, "fixed_baseline"


def build_house_view_prompt_block() -> str:
    """プロンプトへ注入する運用者ハウスビューのブロックを返す。"""
    hv = load_house_view()
    if not hv or not hv.content.strip():
        return (
            "【運用者ハウスビュー】\n"
            "- 未設定。固定ベースライン（古い可能性あり）を背景に用いる。\n"
            "- 当日のニュース見出しと業種別空売りデータから支配的マクロ背景を推定すること。"
        )
    if hv.is_stale():
        freshness = (
            f"⚠️ 最終更新 {hv.updated_label()}（約{hv.age_days}日前・鮮度低下。"
            "ニュース見出しと矛盾する場合はニュースを優先し、その旨を明記）"
        )
    else:
        freshness = f"最終更新 {hv.updated_label()}"
    return (
        "【運用者ハウスビュー（相場観アンカー）】\n"
        f"- {freshness}\n"
        f"{hv.content.strip()}\n"
        "- これを支配的マクロ背景の起点とし、当日のニュース見出し・業種別空売りデータと\n"
        "  『整合するか／反するか』を theme_shift_analysis と theme_sector_alignment で\n"
        "  必ず突合すること。反する場合は条件付きで再評価し、根拠を明示する。"
    )
