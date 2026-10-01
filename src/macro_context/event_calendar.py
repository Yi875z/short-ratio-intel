"""市場イベント・カレンダー（空売り比率の解釈用の窓口）。

イベントの中身（日付・名称・重要度・フェーズ・表記）は JPX_Analysis_System と共通の
正本 market_events.py にある。ここは空売り比率の解釈に必要な見せ方だけを持つ:

- 対象日の前後（既定: 10日前〜21日後）の窓で取り出す。過去側も見るのは、
  通過直後の巻き戻し（FOMC後・SQ後・リバランス後）を読むため。
- プロンプトには相対日（n日後/n日前）と、空売り比率特有の解釈ルールを添える。

イベントを追加・修正するときは market_events.py（正本）を直し、JPX 側で
`python scripts/sync_market_events.py` を実行して写しを揃える。
"""
from __future__ import annotations

from datetime import datetime

from src.macro_context.market_events import (  # noqa: F401  (旧来の import 先を保つ)
    PHASE_LABELS,
    MarketEvent,
    earnings_season_label,
    event_sort_key,
    format_event_line,
    get_events_around,
    get_events_for_month,
    last_business_day,
    sq_date,
)


def get_events_for_date(
    target_date: str,
    before_days: int = 10,
    after_days: int = 21,
) -> list[MarketEvent]:
    """対象日の前後 [before_days, after_days] にある市場イベントを返す。"""
    return get_events_around(target_date, before_days, after_days)


def build_event_calendar_prompt_block(target_date: str, max_lines: int = 30) -> str:
    """プロンプトへ注入する市場イベント・カレンダーのブロックを返す。

    low重要度（GPIFウォッチ・企業物価・GDP2次速報等）はノイズになるため除外する。
    行の表記は JPX_Analysis_System と共通（format_event_line）。
    """
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "【市場イベント・カレンダー】\n- 対象日の解釈に失敗。"

    events = [e for e in get_events_for_date(target_date) if e.importance != "low"]
    if len(events) > max_lines:
        highs = [e for e in events if e.importance == "high"]
        others = [e for e in events if e.importance != "high"]
        events = sorted(highs + others[: max(0, max_lines - len(highs))], key=event_sort_key)

    lines = [
        "【市場イベント・カレンダー（対象日基準・事前に決まった予定）】",
        "※表記: 日付（曜日・対象日からの相対日）[地域/重要度/フェーズ / 時間帯] 名称: 意味。"
        "指数イベントは発表・パッシブ実売買・指数発効を分けて表示。",
    ]

    season = earnings_season_label(target)
    if season:
        lines.append(f"- 決算期: {season}（決算反応の一過性フローに注意）")

    if not events:
        lines.append("- 対象日前後に主要な予定イベントは検出されず。")
    else:
        lines.extend(format_event_line(e, target) for e in events)

    lines.append(
        "- 解釈ルール: SQ週・先物ロール・指数リバランスの実売買日近辺では、"
        "その他(33業種外)や価格規制なし比率の上昇を機械的フローとして扱い、"
        "方向性売り（弱気）と断定しない。指数の結果発表日以降は採用・除外銘柄への"
        "先回りの空売り・買い戻しが増える。FOMC・日銀会合の直前はリスク回避の積み増し、"
        "通過後は巻き戻しが起きやすい。配当権利落ち日は配当再投資（先物買い）で需給が歪む。"
    )
    return "\n".join(lines)
