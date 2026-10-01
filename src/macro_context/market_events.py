"""市場イベント・カレンダーの共通正本（short-ratio-intel / JPX_Analysis_System 共用）。

このファイルは2つのシステムで**同一内容**を使う。
- 正本: short-ratio-intel/src/macro_context/market_events.py
- 写し: JPX_Analysis_System/core/market_events.py
写しは直接編集しない。正本を直してから JPX 側で `python scripts/sync_market_events.py`
を実行する（JPX のテストが正本との一致を検査する）。

方針:
- 標準ライブラリだけで動かす。両リポの依存を増やさないため zoneinfo も使わない
  （Windows では tzdata が別途必要になる）。米東部時間→日本時間は夏時間ルールで換算する。
- 公式に公表された日付だけを手で登録する（CURATED_SERIES）。推測では埋めない。
  登録が尽きかけたら short-ratio の pipeline_health が鳴らす。
- 公式ルールブックから機械的に導ける予定（SQ・指数リバランス・ISM・配当権利落ち）は
  計算で生成する。2026年は公式発表で確定済みの日付を優先し、他年はルール計算の目安を使う。
- 指数イベントは 発表 → パッシブ実売買 → 発効 のフェーズに分けて返す。
- 表記は format_event_line() の1つだけ。両システムのプロンプトはこれを使う。
- 営業日は土日だけを除く簡易判定（日本の祝日は未考慮）。米国営業日は連邦祝日を
  ルールで計算する（年をまたいでも手更新が要らない）。

category（画面の色分け・フィルタ用の粗い分類）:
  macro / derivatives / index_rebalance / dividend / politics / gpif_watch
  （JPX 側だけが fed_speak / position_watch を追加で生成する）
kind（指標の種類。細かい分類。fomc / boj / cpi / nfp / sq / roll / tankan など）
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Iterable, Optional, Union

CALENDAR_VERSION = "2026-10-01"

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

PHASE_LABELS = {
    "announcement": "発表",
    "passive_trade": "実売買",
    "effective": "発効",
    "base_date": "基準",
    "watch": "注意",
    "event": "予定",
    # 実測されたイベント前ポジション調整の監視（JPX の core/event_watch.py が生成）
    "position_watch": "調整監視",
}

PHASE_SORT = {
    "passive_trade": 0,
    "effective": 1,
    "announcement": 2,
    "base_date": 3,
    "watch": 4,
    "event": 5,
    # 本体イベントの下に出す。監視は本体より前に読ませない。
    "position_watch": 6,
}

IMPORTANCE_SORT = {"high": 0, "medium": 1, "low": 2}

MAJOR_SQ_MONTHS = (3, 6, 9, 12)

DateLike = Union[date, datetime, str, None]


@dataclass(frozen=True)
class MarketEvent:
    event_date: date
    name: str
    category: str
    region: str
    importance: str = "medium"
    note: str = ""
    rule: str = ""
    source: str = ""
    phase: str = "event"
    event_group: str = ""
    session: str = ""
    # イベント前ポジション調整の監視だけが持つ辞書参照キー（"SQ|当日" 等。JPX 専用）
    watch_key: str = ""
    kind: str = ""

    def relation_label(self, target: date) -> str:
        delta = (self.event_date - target).days
        if delta == 0:
            return "当日"
        if delta > 0:
            return f"{delta}日後"
        return f"{-delta}日前"

    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)

    def weekday_label(self) -> str:
        return WEEKDAY_JA[self.event_date.weekday()]


def parse_date(value: DateLike) -> Optional[date]:
    """date / datetime / 'YYYY-MM-DD' を date にする。読めなければ None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 営業日ヘルパー（土日除外の簡易版）
# ---------------------------------------------------------------------------

def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """その月の第n weekday（月=0 .. 日=6）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    d = first + timedelta(days=offset + 7 * (n - 1))
    if d.month != month:
        raise ValueError(f"{year}-{month} has fewer than {n} weekday={weekday}")
    return d


def last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, _calendar.monthrange(year, month)[1])
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def last_business_day(year: int, month: int) -> date:
    d = date(year, month, _calendar.monthrange(year, month)[1])
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def first_business_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def nth_business_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if is_business_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"{year}-{month} has fewer than {n} business days")


def business_day_before(d: date) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def business_days_before(d: date, n: int) -> date:
    for _ in range(n):
        d = business_day_before(d)
    return d


def next_business_day_after(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def sq_date(year: int, month: int) -> date:
    """SQ算出日（第2金曜）。"""
    return nth_weekday(year, month, 4, 2)


# ---------------------------------------------------------------------------
# 米国の連邦祝日と時刻換算
# ---------------------------------------------------------------------------

def _observed(d: date) -> date:
    """土曜の祝日は前の金曜、日曜の祝日は翌月曜に振り替える（連邦政府の規則）。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=16)
def us_federal_holidays(year: int) -> frozenset:
    return frozenset({
        _observed(date(year, 1, 1)),        # New Year's Day
        nth_weekday(year, 1, 0, 3),         # Martin Luther King Jr. Day
        nth_weekday(year, 2, 0, 3),         # Washington's Birthday
        last_weekday(year, 5, 0),           # Memorial Day
        _observed(date(year, 6, 19)),       # Juneteenth
        _observed(date(year, 7, 4)),        # Independence Day
        nth_weekday(year, 9, 0, 1),         # Labor Day
        nth_weekday(year, 10, 0, 2),        # Columbus Day
        _observed(date(year, 11, 11)),      # Veterans Day
        nth_weekday(year, 11, 3, 4),        # Thanksgiving
        _observed(date(year, 12, 25)),      # Christmas
    })


def is_us_business_day(d: date) -> bool:
    return is_business_day(d) and d not in us_federal_holidays(d.year)


def nth_us_business_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if is_us_business_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"{year}-{month} has fewer than {n} US business days")


def _us_dst(d: date) -> bool:
    """米国の夏時間（3月第2日曜〜11月第1日曜）。"""
    return nth_weekday(d.year, 3, 6, 2) <= d < nth_weekday(d.year, 11, 6, 1)


def us_time_rule(d: date, label: str, hour: int, minute: int = 0) -> str:
    """'BLS公式リリース日（8:30 ET / 日本時間 21:30 JST）' のような時刻つき規則文。"""
    eastern = datetime.combine(d, time(hour, minute))
    jst = eastern + timedelta(hours=13 if _us_dst(d) else 14)
    next_day = "翌日 " if jst.date() > d else ""
    return f"{label}（{hour}:{minute:02d} ET / 日本時間 {next_day}{jst:%H:%M} JST）"


# ---------------------------------------------------------------------------
# 公式日程の手動登録（推測で埋めない。尽きたら pipeline_health が鳴らす）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CuratedSeries:
    """公式発表済みの日付を持つ指標の系列。

    dates の値は名称、または (名称, 重要度) の組（回によって重要度が違う米GDP等）。
    us_time があれば規則文に米東部→日本時間の換算を付ける。
    recurring=False の単発イベント（選挙等）は「登録が尽きた」検査の対象外。
    """
    kind: str
    category: str
    region: str
    importance: str
    note: str
    rule: str
    source: str
    dates: dict = field(default_factory=dict)
    us_time: Optional[tuple] = None
    recurring: bool = True


CURATED_SERIES: tuple = (
    CuratedSeries(
        "fomc", "macro", "US", "high",
        "米金利・ドル円・グロース株の最大級の振れ要因。SEP付き会合はドットプロットへの感応度が高い。"
        "直前はリスク回避のヘッジ・ショートが積み上がり、通過後は巻き戻しが出やすい。",
        "会合最終日の声明発表", "Federal Reserve FOMC calendars",
        {
            date(2026, 1, 28): "FOMC",
            date(2026, 3, 18): "FOMC（SEP・ドットプロット公表）",
            date(2026, 4, 29): "FOMC",
            date(2026, 6, 17): "FOMC（SEP・ドットプロット公表）",
            date(2026, 7, 29): "FOMC",
            date(2026, 9, 16): "FOMC（SEP・ドットプロット公表）",
            date(2026, 10, 28): "FOMC",
            date(2026, 12, 9): "FOMC（SEP・ドットプロット公表）",
        },
        us_time=(14, 0),
    ),
    CuratedSeries(
        "boj", "macro", "JP", "high",
        "国内金利・銀行/保険株・ドル円に直結。展望レポート月は物価・政策パスへの注目度が高い。"
        "利上げ思惑で銀行株の需給が振れやすい。",
        "会合最終日。結果は東京の日中（概ね正午前後、遅いと後場）に公表",
        "BOJ Monetary Policy Meetings schedule",
        {
            date(2026, 1, 23): "日銀 金融政策決定会合（展望レポート）",
            date(2026, 3, 19): "日銀 金融政策決定会合",
            date(2026, 4, 28): "日銀 金融政策決定会合（展望レポート）",
            date(2026, 6, 16): "日銀 金融政策決定会合",
            date(2026, 7, 31): "日銀 金融政策決定会合（展望レポート）",
            date(2026, 9, 18): "日銀 金融政策決定会合",
            date(2026, 10, 30): "日銀 金融政策決定会合（展望レポート）",
            date(2026, 12, 18): "日銀 金融政策決定会合",
        },
    ),
    CuratedSeries(
        "cpi", "macro", "US", "high",
        "米インフレの中心指標。米金利・ドル円・グロース株の振れ要因。",
        "BLS公式リリース日", "BLS CPI release schedule",
        {
            date(2026, 1, 13): "米CPI（12月分）",
            date(2026, 2, 13): "米CPI（1月分）",
            date(2026, 3, 11): "米CPI（2月分）",
            date(2026, 4, 10): "米CPI（3月分）",
            date(2026, 5, 12): "米CPI（4月分）",
            date(2026, 6, 10): "米CPI（5月分）",
            date(2026, 7, 14): "米CPI（6月分）",
            date(2026, 8, 12): "米CPI（7月分）",
            date(2026, 9, 11): "米CPI（8月分）",
            date(2026, 10, 14): "米CPI（9月分）",
            date(2026, 11, 10): "米CPI（10月分）",
            date(2026, 12, 10): "米CPI（11月分）",
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        "ppi", "macro", "US", "medium",
        "米インフレの川上指標。CPIとセットで金利観を確認。",
        "BLS公式リリース日", "BLS PPI release schedule",
        {
            date(2026, 1, 14): "米PPI（11月分）",
            date(2026, 1, 30): "米PPI（12月分）",
            date(2026, 2, 27): "米PPI（1月分）",
            date(2026, 3, 18): "米PPI（2月分）",
            date(2026, 4, 14): "米PPI（3月分）",
            date(2026, 5, 13): "米PPI（4月分）",
            date(2026, 6, 11): "米PPI（5月分）",
            date(2026, 7, 15): "米PPI（6月分）",
            date(2026, 8, 13): "米PPI（7月分）",
            date(2026, 9, 10): "米PPI（8月分）",
            date(2026, 10, 15): "米PPI（9月分）",
            date(2026, 11, 13): "米PPI（10月分）",
            date(2026, 12, 15): "米PPI（11月分）",
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        # 2026年2月分（1月分）は政府閉鎖で 2/6→2/11 に変更された実績日
        "nfp", "macro", "US", "high",
        "米労働需給の最重要指標。米金利・ドル円の振れ要因。",
        "BLS公式リリース日", "BLS Employment Situation release schedule",
        {
            date(2026, 1, 9): "米雇用統計（12月分）",
            date(2026, 2, 11): "米雇用統計（1月分）",
            date(2026, 3, 6): "米雇用統計（2月分）",
            date(2026, 4, 3): "米雇用統計（3月分）",
            date(2026, 5, 8): "米雇用統計（4月分）",
            date(2026, 6, 5): "米雇用統計（5月分）",
            date(2026, 7, 2): "米雇用統計（6月分）",
            date(2026, 8, 7): "米雇用統計（7月分）",
            date(2026, 9, 4): "米雇用統計（8月分）",
            date(2026, 10, 2): "米雇用統計（9月分）",
            date(2026, 11, 6): "米雇用統計（10月分）",
            date(2026, 12, 4): "米雇用統計（11月分）",
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        "pce", "macro", "US", "high",
        "FRBが重視するインフレ指標。米金利・グロース株の反応に注意。",
        "BEA公式リリース日", "BEA news release schedule: Personal Income and Outlays",
        {
            date(2026, 1, 22): "米PCE・個人所得（10・11月分）",
            date(2026, 2, 20): "米PCE・個人所得（12月分）",
            date(2026, 3, 13): "米PCE・個人所得（1月分）",
            date(2026, 4, 9): "米PCE・個人所得（2月分）",
            date(2026, 4, 30): "米PCE・個人所得（3月分）",
            date(2026, 5, 28): "米PCE・個人所得（4月分）",
            date(2026, 6, 25): "米PCE・個人所得（5月分）",
            date(2026, 7, 30): "米PCE・個人所得（6月分）",
            date(2026, 8, 26): "米PCE・個人所得（7月分）",
            date(2026, 9, 30): "米PCE・個人所得（8月分）",
            date(2026, 10, 29): "米PCE・個人所得（9月分）",
            date(2026, 11, 25): "米PCE・個人所得（10月分）",
            date(2026, 12, 23): "米PCE・個人所得（11月分）",
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        "gdp", "macro", "US", "medium",
        "米国成長率の公式推計。速報値は株価指数・金利・為替への感応度が高い。",
        "BEA公式リリース日", "BEA news release schedule: GDP",
        {
            date(2026, 1, 22): ("米GDP（2025年3Q改定値）", "medium"),
            date(2026, 2, 20): ("米GDP（2025年4Q速報）", "high"),
            date(2026, 3, 13): ("米GDP（2025年4Q改定値）", "medium"),
            date(2026, 4, 9): ("米GDP（2025年4Q確報）", "medium"),
            date(2026, 4, 30): ("米GDP（2026年1Q速報）", "high"),
            date(2026, 5, 28): ("米GDP（2026年1Q改定値）", "medium"),
            date(2026, 6, 25): ("米GDP（2026年1Q確報）", "medium"),
            date(2026, 7, 30): ("米GDP（2026年2Q速報）", "high"),
            date(2026, 8, 26): ("米GDP（2026年2Q改定値）", "medium"),
            date(2026, 9, 30): ("米GDP（2026年2Q確報）", "medium"),
            date(2026, 10, 29): ("米GDP（2026年3Q速報）", "high"),
            date(2026, 11, 25): ("米GDP（2026年3Q改定値）", "medium"),
            date(2026, 12, 23): ("米GDP（2026年3Q確報）", "medium"),
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        "retail", "macro", "US", "high",
        "米個人消費の高頻度指標。消費関連・景気敏感株・金利の反応に注意。",
        "Census Bureau公式リリース日", "U.S. Census Bureau Economic Indicators release schedule",
        {
            date(2026, 1, 14): "米小売売上高（11月分）",
            date(2026, 2, 10): "米小売売上高（12月分）",
            date(2026, 3, 6): "米小売売上高（1月分）",
            date(2026, 4, 1): "米小売売上高（2月分）",
            date(2026, 4, 21): "米小売売上高（3月分）",
            date(2026, 5, 14): "米小売売上高（4月分）",
            date(2026, 6, 17): "米小売売上高（5月分）",
            date(2026, 7, 16): "米小売売上高（6月分）",
            date(2026, 8, 14): "米小売売上高（7月分）",
            date(2026, 9, 16): "米小売売上高（8月分）",
            date(2026, 10, 15): "米小売売上高（9月分）",
            date(2026, 11, 17): "米小売売上高（10月分）",
            date(2026, 12, 16): "米小売売上高（11月分）",
        },
        us_time=(8, 30),
    ),
    CuratedSeries(
        "jolts", "macro", "US", "medium",
        "求人件数・離職率など労働需給の遅行指標。賃金インフレ観測と金利反応に注意。",
        "BLS公式リリース日", "BLS JOLTS release schedule",
        {
            date(2026, 1, 7): "米JOLTS求人件数（11月分）",
            date(2026, 2, 5): "米JOLTS求人件数（12月分）",
            date(2026, 3, 13): "米JOLTS求人件数（1月分）",
            date(2026, 3, 31): "米JOLTS求人件数（2月分）",
            date(2026, 5, 5): "米JOLTS求人件数（3月分）",
            date(2026, 6, 2): "米JOLTS求人件数（4月分）",
            date(2026, 6, 30): "米JOLTS求人件数（5月分）",
            date(2026, 8, 4): "米JOLTS求人件数（6月分）",
            date(2026, 9, 1): "米JOLTS求人件数（7月分）",
            date(2026, 9, 29): "米JOLTS求人件数（8月分）",
            date(2026, 11, 3): "米JOLTS求人件数（9月分）",
            date(2026, 12, 1): "米JOLTS求人件数（10月分）",
        },
        us_time=(10, 0),
    ),
    CuratedSeries(
        "cpi", "macro", "JP", "medium",
        "国内インフレ・日銀の政策観の指標。全国CPI。",
        "総務省統計局公式公表予定（8:30 JST）", "Statistics Bureau of Japan CPI schedule",
        {
            date(2026, 1, 23): "日本CPI（全国・12月分）",
            date(2026, 2, 20): "日本CPI（全国・1月分）",
            date(2026, 3, 24): "日本CPI（全国・2月分）",
            date(2026, 4, 24): "日本CPI（全国・3月分）",
            date(2026, 5, 22): "日本CPI（全国・4月分）",
            date(2026, 6, 19): "日本CPI（全国・5月分）",
            date(2026, 7, 24): "日本CPI（全国・6月分）",
            date(2026, 8, 21): "日本CPI（全国・7月分）",
            date(2026, 9, 18): "日本CPI（全国・8月分）",
            date(2026, 10, 23): "日本CPI（全国・9月分）",
            date(2026, 11, 20): "日本CPI（全国・10月分）",
            date(2026, 12, 18): "日本CPI（全国・11月分）",
            date(2027, 1, 22): "日本CPI（全国・12月分）",
        },
    ),
    CuratedSeries(
        "cpi_tokyo", "macro", "JP", "medium",
        "全国CPIの先行指標として市場が見る東京都区部CPI。",
        "総務省統計局公式公表予定（8:30 JST）", "Statistics Bureau of Japan CPI schedule",
        {
            date(2026, 1, 30): "東京CPI（中旬速報・1月分）",
            date(2026, 2, 27): "東京CPI（中旬速報・2月分）",
            date(2026, 3, 31): "東京CPI（中旬速報・3月分）",
            date(2026, 5, 1): "東京CPI（中旬速報・4月分）",
            date(2026, 5, 29): "東京CPI（中旬速報・5月分）",
            date(2026, 6, 26): "東京CPI（中旬速報・6月分）",
            date(2026, 7, 31): "東京CPI（中旬速報・7月分）",
            date(2026, 8, 28): "東京CPI（中旬速報・8月分）",
            date(2026, 10, 2): "東京CPI（中旬速報・9月分）",
            date(2026, 10, 30): "東京CPI（中旬速報・10月分）",
            date(2026, 11, 27): "東京CPI（中旬速報・11月分）",
            date(2026, 12, 25): "東京CPI（中旬速報・12月分）",
        },
    ),
    CuratedSeries(
        # 内閣府「公表予定」（2026-10-01 取得）。2026年4-6月期の速報は未収録（過去分）。
        "gdp", "macro", "JP", "medium",
        "国内景気の公式推計。日銀の政策観・銀行株・内需株に波及。2次速報は改定幅が焦点。",
        "内閣府公式公表予定（8:50 JST）", "Cabinet Office (ESRI) GDP release schedule",
        {
            date(2026, 2, 16): "日本GDP 1次速報（10-12月期）",
            date(2026, 5, 19): "日本GDP 1次速報（1-3月期）",
            date(2026, 11, 16): "日本GDP 1次速報（7-9月期）",
            date(2026, 12, 8): ("日本GDP 2次速報（7-9月期）", "low"),
            date(2027, 2, 15): "日本GDP 1次速報（10-12月期）",
            date(2027, 3, 9): ("日本GDP 2次速報（10-12月期）", "low"),
        },
    ),
    CuratedSeries(
        # 日銀「統計データの公表・掲載予定（2026年7月～2027年6月）」（2026-10-01 取得）
        "tankan", "macro", "JP", "high",
        "大企業製造業DI・設備投資計画。日銀の政策観と景気敏感株・銀行株の需給に直結。",
        "日本銀行公式公表予定（8:50 JST）", "BOJ statistics release calendar (Tankan)",
        {
            date(2026, 7, 1): "日銀短観（6月調査）",
            date(2026, 10, 1): "日銀短観（9月調査）",
            date(2026, 12, 14): "日銀短観（12月調査）",
            date(2027, 4, 1): "日銀短観（3月調査）",
        },
    ),
    CuratedSeries(
        # 同上。祝日で「第8営業日」の目安からずれる月がある（2026年10月は 10/13）。
        "cgpi", "macro", "JP", "low",
        "国内の川上インフレ指標（企業物価指数）。輸入物価・円安の転嫁を確認。",
        "日本銀行公式公表予定（8:50 JST）", "BOJ statistics release calendar (CGPI)",
        {
            date(2026, 7, 10): "日本 企業物価指数（6月分）",
            date(2026, 8, 13): "日本 企業物価指数（7月分）",
            date(2026, 9, 11): "日本 企業物価指数（8月分）",
            date(2026, 10, 13): "日本 企業物価指数（9月分）",
            date(2026, 11, 12): "日本 企業物価指数（10月分）",
            date(2026, 12, 10): "日本 企業物価指数（11月分）",
            date(2027, 1, 14): "日本 企業物価指数（12月分）",
            date(2027, 2, 10): "日本 企業物価指数（1月分）",
            date(2027, 3, 10): "日本 企業物価指数（2月分）",
            date(2027, 4, 12): "日本 企業物価指数（3月分）",
            date(2027, 5, 17): "日本 企業物価指数（4月分）",
            date(2027, 6, 10): "日本 企業物価指数（5月分）",
        },
    ),
    CuratedSeries(
        "election", "politics", "US", "high",
        "上下院の勢力次第で財政・規制期待が変わる。直前はヘッジのショートが積み上がり、"
        "結果判明後に巻き戻されやすい。",
        "投票日（11月第1月曜の翌火曜）。大勢判明は日本時間の翌日日中",
        "U.S. federal election day (2 U.S.C. §7)",
        {date(2026, 11, 3): "米中間選挙"},
        recurring=False,
    ),
)


def _curated_events(year: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for series in CURATED_SERIES:
        for d, value in series.dates.items():
            if d.year != year:
                continue
            name, importance = value if isinstance(value, tuple) else (value, series.importance)
            rule = us_time_rule(d, series.rule, *series.us_time) if series.us_time else series.rule
            events.append(MarketEvent(
                d, name, series.category, series.region, importance,
                series.note, rule, series.source, kind=series.kind,
            ))
    return events


def curated_coverage() -> list[tuple[CuratedSeries, date]]:
    """定期系列ごとの最終登録日。登録切れの検出（pipeline_health）に使う。"""
    return [
        (series, max(series.dates))
        for series in CURATED_SERIES
        if series.recurring and series.dates
    ]


# ---------------------------------------------------------------------------
# 計算イベント: ISM・SQ/ロール・配当権利落ち
# ---------------------------------------------------------------------------

def _ism_events(year: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for month in range(1, 13):
        manufacturing = nth_us_business_day(year, month, 1)
        services = nth_us_business_day(year, month, 3)
        events.append(MarketEvent(
            manufacturing, "米ISM製造業PMI", "macro", "US", "medium",
            "製造業景況感。50を境に拡大/縮小を判断し、金利・景気敏感株に影響しやすい。",
            us_time_rule(manufacturing, "毎月第1米国営業日", 10, 0),
            "ISM Report On Business release rule", kind="ism",
        ))
        events.append(MarketEvent(
            services, "米ISMサービス業PMI", "macro", "US", "medium",
            "米国GDPの大半を占めるサービス業の景況感。インフレ項目・雇用項目にも注意。",
            us_time_rule(services, "毎月第3米国営業日", 10, 0),
            "ISM Report On Business release rule", kind="ism",
        ))
    return events


def _derivatives_events(year: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    source = "JPX/OSE derivatives contract specifications"
    for month in range(1, 13):
        sq = sq_date(year, month)
        if month in MAJOR_SQ_MONTHS:
            next_month = month % 12 + 3
            events.append(MarketEvent(
                sq, f"メジャーSQ（{month}月限・先物・オプション）", "derivatives", "JP", "high",
                "先物・オプションが同時に満期を迎える。週前半は先物ロールとヘッジ調整で"
                "インデックス連動の機械的な売買が膨らみやすい。",
                "毎月第2金曜日。3/6/9/12月は先物限月も重なるメジャーSQ", source,
                event_group=f"sq-{year}-{month:02d}", kind="sq",
            ))
            events.append(MarketEvent(
                sq - timedelta(days=2), f"先物ロール集中ウォッチ（{month}月限→{next_month}月限）",
                "derivatives", "JP", "medium",
                "期近から期先への建玉移行が出やすい時期。ロールに伴う裁定・スプレッド取引で"
                "機械的な売買が増える。実際のロールは建玉・出来高で確認。",
                "メジャーSQの2日前を目安", source, phase="watch",
                event_group=f"sq-{year}-{month:02d}", kind="roll",
            ))
        else:
            events.append(MarketEvent(
                sq, f"マイナーSQ（{month}月限・オプション）", "derivatives", "JP", "medium",
                "日経225オプションだけの満期。インデックス連動の機械的な売買がやや増える。",
                "毎月第2金曜日", source,
                event_group=f"sq-{year}-{month:02d}", kind="sq",
            ))
    return events


def _dividend_events(year: int) -> list[MarketEvent]:
    """配当権利落ち日（3月本決算・9月中間の集中）。権利確定日=月末最終営業日、
    権利落ち日=その1営業日前（T+2決済）。"""
    events: list[MarketEvent] = []
    for month in (3, 9):
        record = last_business_day(year, month)
        ex_date = business_day_before(record)
        events.append(MarketEvent(
            ex_date, f"配当権利落ち日（{month}月末・集中）", "dividend", "JP", "medium",
            "指数は配当分だけギャップダウンし、権利落ち直後は配当再投資（先物買い）や"
            "裁定解消で需給が一過性に歪む。",
            f"権利確定日（{record.month}/{record.day}）の1営業日前（T+2決済）",
            "JPX trading rules (settlement T+2)", kind="dividend",
        ))
    return events


# ---------------------------------------------------------------------------
# 指数リバランス（2026年は公式確定日、他年はルール計算の目安）
# ---------------------------------------------------------------------------

def _msci_events(year: int) -> list[MarketEvent]:
    """MSCI 定期レビュー。5月・11月=半期（規模大）、2月・8月=四半期。"""
    fixed_2026 = {
        2: (date(2026, 2, 10), date(2026, 2, 27), date(2026, 3, 2)),
        5: (date(2026, 5, 12), date(2026, 5, 29), date(2026, 6, 1)),
        8: (date(2026, 8, 12), date(2026, 8, 31), date(2026, 9, 1)),
        11: (date(2026, 11, 11), date(2026, 11, 30), date(2026, 12, 1)),
    }
    events: list[MarketEvent] = []
    for month in (2, 5, 8, 11):
        semi = month in (5, 11)
        review = "半期レビュー" if semi else "四半期レビュー"
        importance = "high" if semi else "medium"
        if year == 2026:
            announcement, trade, effective = fixed_2026[month]
        else:
            trade = last_business_day(year, month)
            effective = next_business_day_after(trade)
            announcement = business_days_before(trade, 10)  # 実施の約2週間前（目安）
        group = f"msci-{year}-{month:02d}"
        events.append(MarketEvent(
            announcement, f"MSCI {review} 結果発表", "index_rebalance", "Global", importance,
            "採用・除外・ウェイト変更銘柄の公表。発表後は対象銘柄の先回り売買・貸株需給が動き出す。",
            "公式発表日。一般ルールは実施日の少なくとも2週間前",
            "MSCI Global Investable Market Indexes methodology / MSCI index review dates",
            phase="announcement", event_group=group, session="official release", kind="msci",
        ))
        events.append(MarketEvent(
            trade, f"MSCI {review} パッシブ実売買日", "index_rebalance", "Global", "high",
            "レビュー月最終営業日の引けにパッシブ売買が集中する実務日。出来高・比率の急変は"
            "指数入替フローで説明でき、方向性の売り買いと断定しない。",
            "2/5/8/11月の最終営業日引けに実施",
            "MSCI Global Investable Market Indexes methodology",
            phase="passive_trade", event_group=group, session="review month close", kind="msci",
        ))
        events.append(MarketEvent(
            effective, f"MSCI {review} 指数発効日", "index_rebalance", "Global", importance,
            "前営業日引け後の変更が指数に反映される日。寄り付き以降の残需給・逆流を確認。",
            "実施日翌営業日の指数反映",
            "MSCI Global Investable Market Indexes methodology",
            phase="effective", event_group=group, session="next open", kind="msci",
        ))
    return events


def _ftse_events(year: int) -> list[MarketEvent]:
    """FTSE GEIS。3月・9月=半期、6月・12月=四半期。"""
    fixed_2026 = {
        3: (date(2026, 3, 6), date(2026, 3, 19), date(2026, 3, 23),
            "2026年3月は第3金曜が日本の祝日（春分の日）のため、日本株は前営業日3/19引けが実売買目安。"),
        6: (date(2026, 6, 5), date(2026, 6, 19), date(2026, 6, 22), ""),
        9: (date(2026, 9, 4), date(2026, 9, 18), date(2026, 9, 21), ""),
        12: (date(2026, 12, 4), date(2026, 12, 18), date(2026, 12, 21), ""),
    }
    events: list[MarketEvent] = []
    for month in (3, 6, 9, 12):
        semi = month in (3, 9)
        review = "半期レビュー" if semi else "四半期レビュー"
        importance = "high" if semi else "medium"
        if year == 2026:
            final_file, trade, effective, extra = fixed_2026[month]
        else:
            final_file = nth_weekday(year, month, 4, 1)
            trade = nth_weekday(year, month, 4, 3)
            effective = next_business_day_after(trade)
            extra = "祝日未考慮の目安。対象市場の休場日は公式ファイルで確認。"
        group = f"ftse-geis-{year}-{month:02d}"
        events.append(MarketEvent(
            final_file, f"FTSE GEIS {review} 最終ファイル公表", "index_rebalance", "Global",
            importance,
            "採用・除外・株数・浮動株比率変更の確認起点。",
            "レビュー月の第1金曜にFinal review filesを公表", "FTSE Russell GEIS FAQ 2026",
            phase="announcement", event_group=group, session="final files", kind="ftse",
        ))
        events.append(MarketEvent(
            trade, f"FTSE GEIS {review} パッシブ実売買日", "index_rebalance", "JP", "high",
            f"レビュー月の第3金曜引けにパッシブ売買が集中しやすい実務日。{extra}".rstrip(),
            "レビュー月の第3金曜引け後に変更を実施",
            "FTSE Russell GEIS FAQ 2026 / GEIS Ground Rules",
            phase="passive_trade", event_group=group, session="review Friday close", kind="ftse",
        ))
        events.append(MarketEvent(
            effective, f"FTSE GEIS {review} 指数発効日", "index_rebalance", "Global", importance,
            "第3金曜引け後の変更が、翌営業日から指数に反映される日。",
            "レビュー月の第3金曜翌営業日の寄り付きから有効",
            "FTSE Russell GEIS FAQ 2026 / GEIS Ground Rules",
            phase="effective", event_group=group, session="next open", kind="ftse",
        ))
    return events


def _nikkei225_events(year: int) -> list[MarketEvent]:
    """日経平均の定期入替（4月・10月の年2回）。"""
    if year == 2026:
        reviews = [
            ("春季", date(2026, 3, 5), date(2026, 3, 31), date(2026, 4, 1), True),
            ("秋季", date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1), False),
        ]
    else:
        reviews = []
        for month, season in ((4, "春季"), (10, "秋季")):
            effective = first_business_day(year, month)
            trade = business_day_before(effective)
            reviews.append((season, first_business_day(year, month - 1), trade, effective, False))
    source = "Nikkei 225 profile / Nikkei 225 constituent change release"
    events: list[MarketEvent] = []
    for season, announcement, trade, effective, confirmed in reviews:
        group = f"nikkei225-{year}-{season}"
        events.append(MarketEvent(
            announcement, f"日経平均 {season}定期入替 {'結果発表' if confirmed else '発表ウォッチ'}",
            "index_rebalance", "JP", "high" if confirmed else "medium",
            "採用・除外銘柄の公表。発表直後から採用候補の買い・除外候補の売りが先回りで動く。"
            + ("" if confirmed else "未発表のため日程は目安。日経の公式リリースで確認。"),
            "日経平均公式リリース。2026年春は3/5発表、4/1算出から入替", source,
            phase="announcement" if confirmed else "watch", event_group=group,
            session="official release" if confirmed else "release watch", kind="nikkei225",
        ))
        events.append(MarketEvent(
            trade, f"日経平均 {season}定期入替 パッシブ実売買日", "index_rebalance", "JP", "high",
            "指数反映前営業日の大引けに日経平均連動パッシブの売買が集中する実務日。",
            "指数発効前営業日引けを実売買目安", source,
            phase="passive_trade", event_group=group, session="previous close", kind="nikkei225",
        ))
        events.append(MarketEvent(
            effective, f"日経平均 {season}定期入替 指数発効日", "index_rebalance", "JP", "high",
            "入替が日経平均の算出に反映される日。寄り後の残需給と裁定解消を確認。",
            "4月・10月の定期見直し適用日。実日付は公式リリース確認", source,
            phase="effective", event_group=group, session="index calculation", kind="nikkei225",
        ))
    return events


def _topix_events(year: int) -> list[MarketEvent]:
    """TOPIX 定期見直し（10月末）と移行措置（2026年10月〜2028年7月の四半期末）。"""
    source = "TSE Index Guidebook (TOPIX)"
    group = f"topix-periodic-{year}"
    effective = last_business_day(year, 10)
    events: list[MarketEvent] = [
        MarketEvent(
            last_business_day(year, 8), "TOPIX 定期見直し基準日", "index_rebalance", "JP", "medium",
            "定期見直しの基準日。", "毎年8月最終営業日", source,
            phase="base_date", event_group=group, kind="topix",
        ),
        MarketEvent(
            nth_business_day(year, 10, 5), "TOPIX 定期見直し結果公表", "index_rebalance", "JP",
            "high", "JPXが結果を公表。対象銘柄の需給が動き出す。", "毎年10月第5営業日", source,
            phase="announcement", event_group=group, session="JPX website", kind="topix",
        ),
        MarketEvent(
            business_day_before(effective), "TOPIX 定期リバランス パッシブ実売買日",
            "index_rebalance", "JP", "high",
            "指数発効前営業日の大引けにTOPIX連動パッシブの売買が集中する実務日。",
            "10月最終営業日の前営業日引けを実売買目安", source,
            phase="passive_trade", event_group=group, session="previous close", kind="topix",
        ),
        MarketEvent(
            effective, "TOPIX 定期リバランス", "index_rebalance", "JP", "high",
            "定期見直しの実施日。2026年から段階的ウェイト調整（移行措置）も重なる。",
            "毎年10月最終営業日", source,
            phase="effective", event_group=group, session="rebalance date", kind="topix",
        ),
    ]
    transition = [
        (2026, 10, "1st", "0.875"), (2027, 1, "2nd", "0.750"), (2027, 4, "3rd", "0.625"),
        (2027, 7, "4th", "0.500"), (2027, 10, "5th", "0.375"), (2028, 1, "6th", "0.250"),
        (2028, 4, "7th", "0.125"), (2028, 7, "8th", "0"),
    ]
    for y, month, stage, factor in transition:
        if y != year:
            continue
        stage_effective = last_business_day(y, month)
        importance = "high" if (y, month) == (2026, 10) else "medium"
        stage_group = f"topix-transition-{y}-{month:02d}"
        events.append(MarketEvent(
            business_day_before(stage_effective), f"TOPIX 移行措置 {stage}段階 パッシブ実売買日",
            "index_rebalance", "JP", importance,
            f"移行措置対象銘柄のウェイト調整前営業日（transition factor {factor}）。",
            "各四半期の最終営業日の前営業日引けを実売買目安", source,
            phase="passive_trade", event_group=stage_group, session="previous close", kind="topix",
        ))
        events.append(MarketEvent(
            stage_effective, f"TOPIX 移行措置 {stage}段階", "index_rebalance", "JP", importance,
            f"四半期最終営業日に対象銘柄のウェイトを段階調整（transition factor {factor}）。",
            "2026年10月から2028年7月まで各四半期の最終営業日", source,
            phase="effective", event_group=stage_group, session="adjustment date", kind="topix",
        ))
    return events


def _jpx_nikkei400_events(year: int) -> list[MarketEvent]:
    """JPX日経400 定期見直し（基準6月末・公表8月第5営業日・実施8月末）。"""
    source = "JPX-Nikkei Index 400 profile"
    group = f"jpx-nikkei400-{year}"
    effective = last_business_day(year, 8)
    return [
        MarketEvent(
            last_business_day(year, 6), "JPX日経400 定期見直し基準日", "index_rebalance", "JP",
            "medium", "定期見直しの基準日。", "毎年6月最終営業日", source,
            phase="base_date", event_group=group, kind="jpx400",
        ),
        MarketEvent(
            nth_business_day(year, 8, 5), "JPX日経400 定期見直し結果公表", "index_rebalance", "JP",
            "high", "構成銘柄の入替結果を公表。", "毎年8月第5営業日", source,
            phase="announcement", event_group=group, session="JPX website", kind="jpx400",
        ),
        MarketEvent(
            business_day_before(effective), "JPX日経400 定期リバランス パッシブ実売買日",
            "index_rebalance", "JP", "high",
            "指数発効前営業日の大引けにJPX日経400連動パッシブの売買が集中する実務日。",
            "8月最終営業日の前営業日引けを実売買目安", source,
            phase="passive_trade", event_group=group, session="previous close", kind="jpx400",
        ),
        MarketEvent(
            effective, "JPX日経400 定期リバランス", "index_rebalance", "JP", "high",
            "構成銘柄の定期見直しを実施。", "毎年8月最終営業日", source,
            phase="effective", event_group=group, session="rebalance date", kind="jpx400",
        ),
    ]


def _jpx_prime150_events(year: int) -> list[MarketEvent]:
    """JPX Prime150 定期見直し。連動パッシブ資産が小さいため重要度は medium に留める。"""
    source = "JPX Prime 150 Index Guidebook"
    group = f"jpx-prime150-{year}"
    effective = last_business_day(year, 8)
    return [
        MarketEvent(
            last_business_day(year, 6), "JPX Prime150 定期見直し基準日", "index_rebalance", "JP",
            "medium", "定期見直しの基準日。", "毎年6月最終営業日", source,
            phase="base_date", event_group=group, kind="prime150",
        ),
        MarketEvent(
            business_days_before(effective, 5), "JPX Prime150 定期見直し結果公表",
            "index_rebalance", "JP", "medium",
            "構成銘柄の入替結果を公表（連動資産は小さく、需給への影響は限定的）。",
            "8月最終営業日の5営業日前", source,
            phase="announcement", event_group=group, session="JPX website", kind="prime150",
        ),
        MarketEvent(
            business_day_before(effective), "JPX Prime150 定期リバランス パッシブ実売買日",
            "index_rebalance", "JP", "medium",
            "指数発効前営業日の大引け。連動パッシブは小さいが個別銘柄の引け需給は確認する。",
            "8月最終営業日の前営業日引けを実売買目安", source,
            phase="passive_trade", event_group=group, session="previous close", kind="prime150",
        ),
        MarketEvent(
            effective, "JPX Prime150 定期リバランス", "index_rebalance", "JP", "medium",
            "定期見直しを実施。", "毎年8月最終営業日", source,
            phase="effective", event_group=group, session="rebalance date", kind="prime150",
        ),
    ]


def _gpif_watch_events(year: int) -> list[MarketEvent]:
    return [
        MarketEvent(
            nth_weekday(year, month, 0, 1), "GPIF関連 指数・ESGパッシブ確認", "gpif_watch", "JP",
            "low",
            "GPIF関連は直接の個別銘柄定期入替ではありません。採用指数、ESG指数、"
            "公募・採用ニュースを確認する運用上の点検日。",
            "四半期初の運用チェック日（公式の直接リバランス日ではない）",
            "GPIF investment / ESG index information pages",
            phase="watch", kind="gpif",
        )
        for month in (1, 4, 7, 10)
    ]


# ---------------------------------------------------------------------------
# 集約と検索
# ---------------------------------------------------------------------------

def event_sort_key(event: MarketEvent) -> tuple:
    return (
        event.event_date,
        PHASE_SORT.get(event.phase, 9),
        IMPORTANCE_SORT.get(event.importance, 9),
        event.region,
        event.name,
    )


@lru_cache(maxsize=8)
def _events_for_year(year: int) -> tuple:
    events: list[MarketEvent] = []
    events.extend(_curated_events(year))
    events.extend(_ism_events(year))
    events.extend(_derivatives_events(year))
    events.extend(_dividend_events(year))
    events.extend(_msci_events(year))
    events.extend(_ftse_events(year))
    events.extend(_nikkei225_events(year))
    events.extend(_topix_events(year))
    events.extend(_jpx_nikkei400_events(year))
    events.extend(_jpx_prime150_events(year))
    events.extend(_gpif_watch_events(year))
    # MSCI 発効日などは翌年にまたがりうるので、その年の日付だけに絞る
    return tuple(sorted((e for e in events if e.event_date.year == year), key=event_sort_key))


def build_market_events(year: int) -> list[MarketEvent]:
    """その年の全イベント（日付・フェーズ・重要度順）。"""
    return list(_events_for_year(year))


def get_events_between(start: date, end: date) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for year in range(start.year, end.year + 1):
        events.extend(e for e in _events_for_year(year) if start <= e.event_date <= end)
    return sorted(events, key=event_sort_key)


def get_events_on(target: DateLike) -> list[MarketEvent]:
    d = parse_date(target)
    return [] if d is None else get_events_between(d, d)


def get_events_around(target: DateLike, before_days: int = 10,
                      after_days: int = 21) -> list[MarketEvent]:
    d = parse_date(target)
    if d is None:
        return []
    return get_events_between(d - timedelta(days=before_days), d + timedelta(days=after_days))


def get_events_for_month(year: int, month: int) -> list[MarketEvent]:
    return [e for e in _events_for_year(year) if e.event_date.month == month]


# 日本の決算集中期（おおよその窓）: (開始月日, 終了月日, ラベル)
_EARNINGS_WINDOWS = [
    ((4, 25), (5, 15), "本決算・通期決算の集中期"),
    ((7, 25), (8, 14), "第1四半期決算の集中期"),
    ((10, 25), (11, 14), "第2四半期決算の集中期"),
    ((1, 25), (2, 14), "第3四半期決算の集中期"),
]


def earnings_season_label(target: date) -> str:
    """対象日が日本の決算集中期に入っていればラベルを返す。"""
    for (sm, sd), (em, ed), label in _EARNINGS_WINDOWS:
        if date(target.year, sm, sd) <= target <= date(target.year, em, ed):
            return label
    return ""


# ---------------------------------------------------------------------------
# 表記（両システム共通）
# ---------------------------------------------------------------------------

def format_event_line(event: MarketEvent, reference: Optional[date] = None) -> str:
    """1イベント1行の共通表記。

    例: - 2026-10-14（水・13日後）[US/high/予定] 米CPI（9月分）: 米インフレの中心指標。…
    reference を渡すと曜日の後に相対日（当日/n日後/n日前）を付ける。
    """
    relation = f"・{event.relation_label(reference)}" if reference else ""
    session = f" / {event.session}" if event.session else ""
    return (
        f"- {event.event_date:%Y-%m-%d}（{event.weekday_label()}{relation}）"
        f"[{event.region}/{event.importance}/{event.phase_label()}{session}] "
        f"{event.name}: {event.note}"
    )


def format_event_lines(events: Iterable[MarketEvent],
                       reference: Optional[date] = None) -> list[str]:
    return [format_event_line(e, reference) for e in events]
