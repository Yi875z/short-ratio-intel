"""市場イベント・カレンダー。

レポート対象日の前後にある「事前に決まった予定」を返し、空売り比率の解釈を
イベント文脈で条件分岐できるようにする。狙いは特に:

- MSCI入替・SQ・先物ロール → インデックス連動の機械的フロー。これらの近辺で
  「その他(33業種外)」や価格規制なし比率が上がっても、方向性売りと混同しない。
- FOMC・日銀会合・主要指標 → 通過前のリスク回避（ショート積み増し）と通過後の
  巻き戻しという時間軸で読む。
- 配当権利落ち・決算期 → 一過性の需給歪みの判別。

計算で確定できる予定（SQ/MSCI/配当/決算期/NFP）はここで生成し、不規則な予定
（FOMC/日銀）は config.market_calendar の手動データを読む。
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from config.market_calendar import CURATED_EVENTS

MAJOR_SQ_MONTHS = {3, 6, 9, 12}      # メジャーSQ（先物＋オプション）
MSCI_SEMIANNUAL_MONTHS = {5, 11}     # MSCI 半期リバランス（規模大）
MSCI_QUARTERLY_MONTHS = {2, 8}       # MSCI 四半期リバランス（規模小）

# 日本の決算集中期（おおよその窓）: (開始月日, 終了月日, ラベル)
_EARNINGS_WINDOWS = [
    ((4, 25), (5, 15), "本決算・通期決算の集中期"),
    ((7, 25), (8, 14), "第1四半期決算の集中期"),
    ((10, 25), (11, 14), "第2四半期決算の集中期"),
    ((1, 25), (2, 14), "第3四半期決算の集中期"),
]


@dataclass(frozen=True)
class MarketEvent:
    name: str
    event_date: date
    category: str        # sq / rollover / msci / dividend / fomc / boj / nfp
    region: str          # JP / US
    importance: str      # high / medium / low
    note: str

    def relation_label(self, target: date) -> str:
        delta = (self.event_date - target).days
        if delta == 0:
            return "当日"
        if delta > 0:
            return f"{delta}日後"
        return f"{-delta}日前"


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """その月の第n weekday を返す（weekday: 月=0 .. 日=6）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def last_business_day(year: int, month: int) -> date:
    """その月の最終営業日（土日を除く。祝日は考慮しない簡易版）を返す。"""
    last = date(year, month, _calendar.monthrange(year, month)[1])
    while last.weekday() >= 5:  # 5=土, 6=日
        last -= timedelta(days=1)
    return last


def sq_date(year: int, month: int) -> date:
    """SQ算出日（第2金曜）。"""
    return nth_weekday(year, month, 4, 2)


def earnings_season_label(target: date) -> str:
    """対象日が日本の決算集中期に入っていればラベルを返す。"""
    for (sm, sd), (em, ed), label in _EARNINGS_WINDOWS:
        start = date(target.year, sm, sd)
        end = date(target.year, em, ed)
        if start <= end and start <= target <= end:
            return label
    return ""


def _computed_events_for_month(year: int, month: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []

    sq = sq_date(year, month)
    if month in MAJOR_SQ_MONTHS:
        events.append(MarketEvent(
            f"メジャーSQ（{month}月限・先物＋オプション）", sq, "sq", "JP", "high",
            "先物・オプションのSQ。週前半は先物ロールとヘッジで規制なし比率・"
            "その他(33業種外)が機械的に膨らみやすい。方向性売りと混同しない。",
        ))
        # 先物の限月交代（ロール）はSQ週前半に集中
        events.append(MarketEvent(
            f"日経225先物 限月交代（{month}月限→翌限）", sq - timedelta(days=2), "rollover", "JP",
            "medium", "ロールに伴うカレンダースプレッド・裁定フローで規制なし比率が上振れしやすい。",
        ))
    else:
        events.append(MarketEvent(
            f"マイナーSQ（{month}月限・オプション）", sq, "sq", "JP", "medium",
            "オプションのみのSQ。インデックス連動の機械的フローがやや増える。",
        ))

    if month in MSCI_SEMIANNUAL_MONTHS:
        events.append(MarketEvent(
            "MSCI 半期リバランス（指数構成見直し・実施）", last_business_day(year, month),
            "msci", "JP", "high",
            "半期の大型リバランス。実施日の引けにかけてパッシブの機械的売買が集中。"
            "その他(33業種外)・規制なし比率の上昇は指数入替フローで説明でき、"
            "方向性売り（弱気）と断定しない。",
        ))
    elif month in MSCI_QUARTERLY_MONTHS:
        events.append(MarketEvent(
            "MSCI 四半期リバランス（指数構成見直し・実施）", last_business_day(year, month),
            "msci", "JP", "medium",
            "四半期の小型リバランス。指数連動の機械的フローが実施日に増える。",
        ))

    # 配当権利落ち（3月本決算・9月中間の集中。最終営業日=権利落ち日の近似）
    if month in {3, 9}:
        ex_date = last_business_day(year, month)
        events.append(MarketEvent(
            "配当権利落ち日（集中）", ex_date, "dividend", "JP", "medium",
            "権利落ち前後は配当再投資・先物ヘッジで需給が一過性に歪む。",
        ))

    # 米雇用統計（NFP）= 目安として原則第1金曜（BLSは月により第2金曜へずれるため要確認）
    events.append(MarketEvent(
        "米雇用統計（NFP・目安）", nth_weekday(year, month, 4, 1), "nfp", "US", "medium",
        "米金利・ドル円の振れ要因。日付は目安（第1金曜）。月により第2金曜にずれるため実日程は要確認。",
    ))

    return events


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """(year, month) を offset ヶ月ずらした (year, month) を返す。"""
    index = year * 12 + (month - 1) + offset
    return index // 12, index % 12 + 1


def _curated_events() -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for iso, name, category, region, importance, note in CURATED_EVENTS:
        try:
            d = datetime.strptime(iso, "%Y-%m-%d").date()
        except ValueError:
            continue
        events.append(MarketEvent(name, d, category, region, importance, note))
    return events


def get_events_for_date(
    target_date: str,
    before_days: int = 10,
    after_days: int = 21,
) -> list[MarketEvent]:
    """対象日の前後 [before_days, after_days] にある市場イベントを返す。"""
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []

    start = target - timedelta(days=before_days)
    end = target + timedelta(days=after_days)

    candidates: list[MarketEvent] = list(_curated_events())
    # 前月・当月・翌月の計算イベントを生成（窓が月境界をまたいでも拾う）
    seen_months: set[tuple[int, int]] = set()
    for offset_month in (-1, 0, 1):
        ym = _shift_month(target.year, target.month, offset_month)
        if ym in seen_months:
            continue
        seen_months.add(ym)
        candidates.extend(_computed_events_for_month(ym[0], ym[1]))

    in_window = [e for e in candidates if start <= e.event_date <= end]
    in_window.sort(key=lambda e: e.event_date)
    return in_window


def get_events_for_month(year: int, month: int) -> list[MarketEvent]:
    """指定月内の全イベント（計算系＋キュレーション）を日付順で返す。月間カレンダー表示用。"""
    events = list(_computed_events_for_month(year, month))
    for event in _curated_events():
        if event.event_date.year == year and event.event_date.month == month:
            events.append(event)
    events.sort(key=lambda e: (e.event_date, e.importance))
    return events


def build_event_calendar_prompt_block(target_date: str) -> str:
    """プロンプトへ注入する市場イベント・カレンダーのブロックを返す。"""
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "【市場イベント・カレンダー】\n- 対象日の解釈に失敗。"

    events = get_events_for_date(target_date)
    lines = ["【市場イベント・カレンダー（対象日基準・事前に決まった予定）】"]

    season = earnings_season_label(target)
    if season:
        lines.append(f"- 決算期: {season}（決算反応の一過性フローに注意）")

    if not events:
        lines.append("- 対象日前後に主要な予定イベントは検出されず。")
    else:
        for e in events:
            mark = "★" if e.importance == "high" else "・"
            lines.append(
                f"{mark} {e.event_date.isoformat()}（{e.relation_label(target)}）"
                f"[{e.region}] {e.name}: {e.note}"
            )

    lines.append(
        "- 解釈ルール: SQ週・先物ロール・MSCI入替の近辺では、その他(33業種外)や"
        "価格規制なし比率の上昇を機械的フローとして扱い、方向性売り（弱気）と断定しない。"
        " FOMC・日銀会合の直前はリスク回避の積み増し、通過後は巻き戻しが起きやすい。"
    )
    return "\n".join(lines)
