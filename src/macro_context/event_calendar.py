"""市場イベント・カレンダー。

レポート対象日の前後にある「事前に決まった予定」を返し、空売り比率の解釈を
イベント文脈で条件分岐できるようにする。狙いは特に:

- 指数リバランス（MSCI/FTSE/日経平均/TOPIX/JPX日経400等）・SQ・先物ロール
  → インデックス連動の機械的フロー。これらの近辺で「その他(33業種外)」や
  価格規制なし比率が上がっても、方向性売りと混同しない。
- FOMC・日銀会合・主要指標 → 通過前のリスク回避（ショート積み増し）と通過後の
  巻き戻しという時間軸で読む。
- 配当権利落ち・決算期 → 一過性の需給歪みの判別。

役割分担:
- 不規則で計算できない予定（中銀会合・経済指標の公式日付）
  → config.market_calendar の手動キュレーション・データを読む。
- 公式ルールブックから機械的に導ける予定（SQ・指数リバランス・配当落ち・ISM等）
  → 本モジュールが計算で生成する。2026年は公式発表で確定済みの日付を優先し、
    それ以外の年はルール計算のフォールバック（目安）を使う。

指数イベントは「発表(announcement)→パッシブ実売買(passive_trade)→発効(effective)」の
フェーズに分けて返す。空売り需給の解釈で最重要なのは実売買日の引け。
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from config.market_calendar import CURATED_EVENTS

MAJOR_SQ_MONTHS = {3, 6, 9, 12}      # メジャーSQ（先物＋オプション）
MSCI_SEMIANNUAL_MONTHS = {5, 11}     # MSCI 半期リバランス（規模大）
MSCI_QUARTERLY_MONTHS = {2, 8}       # MSCI 四半期リバランス（規模小）

# イベントのフェーズ表示ラベル（指数リバランスは3段階に分けて出す）
PHASE_LABELS = {
    "announcement": "発表",
    "passive_trade": "実売買",
    "effective": "発効",
    "base_date": "基準",
    "watch": "注意",
    "event": "予定",
}

_PHASE_SORT = {
    "passive_trade": 0,
    "effective": 1,
    "announcement": 2,
    "base_date": 3,
    "watch": 4,
    "event": 5,
}

_IMPORTANCE_SORT = {"high": 0, "medium": 1, "low": 2}

# 日本の決算集中期（おおよその窓）: (開始月日, 終了月日, ラベル)
_EARNINGS_WINDOWS = [
    ((4, 25), (5, 15), "本決算・通期決算の集中期"),
    ((7, 25), (8, 14), "第1四半期決算の集中期"),
    ((10, 25), (11, 14), "第2四半期決算の集中期"),
    ((1, 25), (2, 14), "第3四半期決算の集中期"),
]

# 米国の連邦祝日（ISM等の「第n米国営業日」計算に使用。年次更新）
_US_FEDERAL_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12),
    date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
}


@dataclass(frozen=True)
class MarketEvent:
    name: str
    event_date: date
    category: str        # sq / rollover / index_rebalance / dividend / fomc / boj / cpi / ...
    region: str          # JP / US / Global
    importance: str      # high / medium / low
    note: str
    phase: str = "event"  # announcement / passive_trade / effective / base_date / watch / event

    def relation_label(self, target: date) -> str:
        delta = (self.event_date - target).days
        if delta == 0:
            return "当日"
        if delta > 0:
            return f"{delta}日後"
        return f"{-delta}日前"

    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)


# ---------------------------------------------------------------------------
# 営業日ヘルパー（土日除外の簡易版。日本の祝日が月末・月初に重ならない限り実用上十分）
# ---------------------------------------------------------------------------

def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """その月の第n weekday を返す（weekday: 月=0 .. 日=6）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _is_business_day(d: date) -> bool:
    return d.weekday() < 5


def last_business_day(year: int, month: int) -> date:
    """その月の最終営業日（土日を除く。祝日は考慮しない簡易版）を返す。"""
    last = date(year, month, _calendar.monthrange(year, month)[1])
    while not _is_business_day(last):
        last -= timedelta(days=1)
    return last


def _business_day_before(d: date) -> date:
    d -= timedelta(days=1)
    while not _is_business_day(d):
        d -= timedelta(days=1)
    return d


def _business_days_before(d: date, n: int) -> date:
    for _ in range(n):
        d = _business_day_before(d)
    return d


def _next_business_day_after(d: date) -> date:
    d += timedelta(days=1)
    while not _is_business_day(d):
        d += timedelta(days=1)
    return d


def _nth_business_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if _is_business_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"{year}-{month} has fewer than {n} business days")


def _first_business_day(year: int, month: int) -> date:
    return _nth_business_day(year, month, 1)


def _is_us_business_day(d: date) -> bool:
    return _is_business_day(d) and d not in _US_FEDERAL_HOLIDAYS


def _nth_us_business_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if _is_us_business_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"{year}-{month} has fewer than {n} US business days")


def sq_date(year: int, month: int) -> date:
    """SQ算出日（第2金曜。祝日の場合は前営業日だが2026年は該当なし）。"""
    return nth_weekday(year, month, 4, 2)


def earnings_season_label(target: date) -> str:
    """対象日が日本の決算集中期に入っていればラベルを返す。"""
    for (sm, sd), (em, ed), label in _EARNINGS_WINDOWS:
        start = date(target.year, sm, sd)
        end = date(target.year, em, ed)
        if start <= end and start <= target <= end:
            return label
    return ""


# ---------------------------------------------------------------------------
# 計算イベント: SQ・先物ロール・配当権利落ち・ISM・日銀短観・企業物価
# ---------------------------------------------------------------------------

def _derivatives_events(year: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for month in range(1, 13):
        sq = sq_date(year, month)
        if month in MAJOR_SQ_MONTHS:
            events.append(MarketEvent(
                f"メジャーSQ（{month}月限・先物＋オプション）", sq, "sq", "JP", "high",
                "先物・オプションのSQ。週前半は先物ロールとヘッジで規制なし比率・"
                "その他(33業種外)が機械的に膨らみやすい。方向性売りと混同しない。",
            ))
            events.append(MarketEvent(
                f"日経225先物 限月交代（{month}月限→翌限）", sq - timedelta(days=2),
                "rollover", "JP", "medium",
                "ロールに伴うカレンダースプレッド・裁定フローで規制なし比率が上振れしやすい。",
                phase="watch",
            ))
        else:
            events.append(MarketEvent(
                f"マイナーSQ（{month}月限・オプション）", sq, "sq", "JP", "medium",
                "オプションのみのSQ。インデックス連動の機械的フローがやや増える。",
            ))
    return events


def _dividend_events(year: int) -> list[MarketEvent]:
    """配当権利落ち日（3月本決算・9月中間の集中）。

    権利確定日=月末最終営業日、権利落ち日=その1営業日前（T+2決済）。
    権利付き最終日はさらに1営業日前。
    """
    events: list[MarketEvent] = []
    for month in (3, 9):
        record = last_business_day(year, month)
        ex_date = _business_day_before(record)
        events.append(MarketEvent(
            "配当権利落ち日（集中）", ex_date, "dividend", "JP", "medium",
            f"権利確定日（{record.month}/{record.day}）の1営業日前。指数は配当分ギャップダウンし、"
            "権利落ち直後は配当再投資（先物買い）や裁定解消で需給が一過性に歪む。",
        ))
    return events


def _ism_events(year: int) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for month in range(1, 13):
        manufacturing = _nth_us_business_day(year, month, 1)
        services = _nth_us_business_day(year, month, 3)
        events.append(MarketEvent(
            "米ISM製造業景況指数", manufacturing, "ism", "US", "medium",
            "毎月第1米国営業日（10:00 ET）。50割れ・急変時は景気敏感株のショートが動きやすい。",
        ))
        events.append(MarketEvent(
            "米ISMサービス業景況指数", services, "ism", "US", "medium",
            "毎月第3米国営業日（10:00 ET）。米GDPの太宗を占めるサービス業の景況感。",
        ))
    return events


def _tankan_events(year: int) -> list[MarketEvent]:
    """日銀短観。4月・7月・10月調査は原則月初、12月調査は中旬公表（目安）。"""
    events: list[MarketEvent] = []
    for month in (4, 7, 10):
        quarter = {4: "3月調査", 7: "6月調査", 10: "9月調査"}[month]
        events.append(MarketEvent(
            f"日銀短観（{quarter}）", _first_business_day(year, month), "tankan", "JP", "high",
            "大企業製造業DI・設備投資計画。日銀の政策観と景気敏感株・銀行株の需給に直結。",
        ))
    events.append(MarketEvent(
        "日銀短観（12月調査・目安）", date(year, 12, 14), "tankan", "JP", "medium",
        "12月調査は中旬公表。日付は目安のため日銀公表予定で要確認。",
        phase="watch",
    ))
    return events


def _jp_ppi_events(year: int) -> list[MarketEvent]:
    """日本の企業物価指数（日銀）。原則翌月第8営業日公表（目安・祝日未考慮）。"""
    return [
        MarketEvent(
            "日本 企業物価指数（日銀・目安）", _nth_business_day(year, month, 8),
            "ppi", "JP", "low",
            "国内の川上インフレ指標。原則第8営業日公表だが祝日で前後するため実日程は要確認。",
        )
        for month in range(1, 13)
    ]


# ---------------------------------------------------------------------------
# 指数リバランス・イベント（2026年は公式確定日、他年はルール計算のフォールバック）
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
        semi = month in MSCI_SEMIANNUAL_MONTHS
        review = "半期レビュー" if semi else "四半期レビュー"
        importance = "high" if semi else "medium"
        if year == 2026:
            announcement, trade, effective = fixed_2026[month]
        else:
            trade = last_business_day(year, month)
            effective = _next_business_day_after(trade)
            announcement = _business_days_before(trade, 10)  # 実施の約2週間前（目安）
        events.append(MarketEvent(
            f"MSCI {review} 結果発表", announcement, "index_rebalance", "Global", importance,
            "採用・除外・ウェイト変更銘柄の公表。発表後は対象銘柄の空売り・貸株需給が動き出す。",
            phase="announcement",
        ))
        events.append(MarketEvent(
            f"MSCI {review} パッシブ実売買日", trade, "index_rebalance", "Global", "high",
            "レビュー月最終営業日の引けにパッシブ売買が集中。その他(33業種外)・規制なし比率の"
            "上昇は指数入替フローで説明でき、方向性売り（弱気）と断定しない。",
            phase="passive_trade",
        ))
        events.append(MarketEvent(
            f"MSCI {review} 指数発効日", effective, "index_rebalance", "Global", importance,
            "前営業日引けの変更が指数に反映。寄り以降の残需給・逆流に注意。",
            phase="effective",
        ))
    return events


def _ftse_events(year: int) -> list[MarketEvent]:
    """FTSE GEIS（グローバル株指数）。3月・9月=半期、6月・12月=四半期。"""
    fixed_2026 = {
        3: (date(2026, 3, 6), date(2026, 3, 19), date(2026, 3, 23),
            "2026年3月は第3金曜が祝日（春分の日）のため日本株は前営業日3/19引けが実売買目安。"),
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
            announcement, trade, effective, extra = fixed_2026[month]
        else:
            announcement = nth_weekday(year, month, 4, 1)   # 第1金曜: 最終ファイル公表
            trade = nth_weekday(year, month, 4, 3)          # 第3金曜引け
            effective = _next_business_day_after(trade)
            extra = "祝日未考慮の目安。対象市場の休場日は公式ファイルで要確認。"
        events.append(MarketEvent(
            f"FTSE GEIS {review} 最終ファイル公表", announcement, "index_rebalance", "Global",
            importance, "採用・除外・株数・浮動株比率変更の確認起点（レビュー月第1金曜）。",
            phase="announcement",
        ))
        events.append(MarketEvent(
            f"FTSE GEIS {review} パッシブ実売買日", trade, "index_rebalance", "JP", "high",
            f"レビュー月第3金曜引けにパッシブ売買が集中しやすい。{extra}",
            phase="passive_trade",
        ))
        events.append(MarketEvent(
            f"FTSE GEIS {review} 指数発効日", effective, "index_rebalance", "Global", importance,
            "第3金曜引け後の変更が翌営業日から指数に反映。",
            phase="effective",
        ))
    return events


def _nikkei225_events(year: int) -> list[MarketEvent]:
    """日経平均の定期入替（年2回: 4月・10月初から算出反映）。"""
    if year == 2026:
        reviews = [
            ("春季", date(2026, 3, 5), date(2026, 3, 31), date(2026, 4, 1), True),
            ("秋季", date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1), False),
        ]
    else:
        reviews = []
        for month, season in ((4, "春季"), (10, "秋季")):
            effective = _first_business_day(year, month)
            trade = _business_day_before(effective)
            watch = _first_business_day(year, month - 1)
            reviews.append((season, watch, trade, effective, False))
    events: list[MarketEvent] = []
    for season, announcement, trade, effective, confirmed in reviews:
        events.append(MarketEvent(
            f"日経平均 {season}定期入替 {'結果発表' if confirmed else '発表ウォッチ'}",
            announcement, "index_rebalance", "JP", "high" if confirmed else "medium",
            "採用・除外銘柄の公表。発表直後から採用候補の買い・除外候補の空売りが先回りで動く。"
            + ("" if confirmed else "（未発表のため日程は目安。日経公式リリースで要確認）"),
            phase="announcement" if confirmed else "watch",
        ))
        events.append(MarketEvent(
            f"日経平均 {season}定期入替 パッシブ実売買日", trade, "index_rebalance", "JP", "high",
            "指数反映前営業日の大引けに日経平均連動パッシブの売買が集中する実務日。",
            phase="passive_trade",
        ))
        events.append(MarketEvent(
            f"日経平均 {season}定期入替 指数発効日", effective, "index_rebalance", "JP", "high",
            "入替が日経平均の算出に反映される日。寄り後の残需給と裁定解消を確認。",
            phase="effective",
        ))
    return events


def _topix_events(year: int) -> list[MarketEvent]:
    """TOPIX 定期見直し（10月末）と移行措置（2026年10月〜2028年7月の四半期末）。"""
    events: list[MarketEvent] = [
        MarketEvent(
            "TOPIX 定期見直し基準日", last_business_day(year, 8), "index_rebalance", "JP",
            "medium", "毎年8月最終営業日が定期見直しの基準日（TOPIX指数ガイドブック）。",
            phase="base_date",
        ),
        MarketEvent(
            "TOPIX 定期見直し結果公表", _nth_business_day(year, 10, 5), "index_rebalance", "JP",
            "high", "毎年10月第5営業日にJPXが結果を公表。対象銘柄の需給が動き出す。",
            phase="announcement",
        ),
    ]
    effective = last_business_day(year, 10)
    events.append(MarketEvent(
        "TOPIX 定期リバランス パッシブ実売買日", _business_day_before(effective),
        "index_rebalance", "JP", "high",
        "指数発効前営業日の大引けにTOPIX連動パッシブの売買が集中する実務日。",
        phase="passive_trade",
    ))
    events.append(MarketEvent(
        "TOPIX 定期リバランス", effective, "index_rebalance", "JP", "high",
        "毎年10月最終営業日に定期見直しを実施（TOPIX指数ガイドブック）。",
        phase="effective",
    ))
    # 移行措置: 2026年10月から2028年7月まで四半期ごとに段階的ウェイト調整
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
        events.append(MarketEvent(
            f"TOPIX 移行措置 {stage}段階 パッシブ実売買日", _business_day_before(stage_effective),
            "index_rebalance", "JP", importance,
            f"移行措置対象銘柄のウェイト調整前営業日（transition factor {factor}）。",
            phase="passive_trade",
        ))
        events.append(MarketEvent(
            f"TOPIX 移行措置 {stage}段階", stage_effective, "index_rebalance", "JP", importance,
            f"四半期最終営業日に対象銘柄のウェイトを段階調整（transition factor {factor}）。",
            phase="effective",
        ))
    return events


def _jpx_nikkei400_events(year: int) -> list[MarketEvent]:
    """JPX日経400 定期見直し（基準6月末・公表8月第5営業日・実施8月末）。"""
    effective = last_business_day(year, 8)
    return [
        MarketEvent(
            "JPX日経400 定期見直し基準日", last_business_day(year, 6), "index_rebalance", "JP",
            "medium", "毎年6月最終営業日が基準日（JPX公式）。", phase="base_date",
        ),
        MarketEvent(
            "JPX日経400 定期見直し結果公表", _nth_business_day(year, 8, 5), "index_rebalance",
            "JP", "high", "毎年8月第5営業日に結果公表（JPX公式）。", phase="announcement",
        ),
        MarketEvent(
            "JPX日経400 定期リバランス パッシブ実売買日", _business_day_before(effective),
            "index_rebalance", "JP", "high",
            "指数発効前営業日の大引けにJPX日経400連動パッシブの売買が集中する実務日。",
            phase="passive_trade",
        ),
        MarketEvent(
            "JPX日経400 定期リバランス", effective, "index_rebalance", "JP", "high",
            "毎年8月最終営業日に構成銘柄の定期見直しを実施（JPX公式）。", phase="effective",
        ),
    ]


def _jpx_prime150_events(year: int) -> list[MarketEvent]:
    """JPX Prime150 定期見直し（基準6月末・公表は実施5営業日前・実施8月末）。

    連動パッシブ資産が小さいため重要度は medium に留める。
    """
    effective = last_business_day(year, 8)
    return [
        MarketEvent(
            "JPX Prime150 定期見直し結果公表", _business_days_before(effective, 5),
            "index_rebalance", "JP", "medium",
            "8月最終営業日の5営業日前に結果公表（ガイドブック）。", phase="announcement",
        ),
        MarketEvent(
            "JPX Prime150 定期リバランス", effective, "index_rebalance", "JP", "medium",
            "毎年8月最終営業日に定期見直しを実施（ガイドブック）。", phase="effective",
        ),
    ]


def _gpif_watch_events(year: int) -> list[MarketEvent]:
    """GPIF関連の運用チェック日（直接のリバランス日ではない点に注意）。"""
    return [
        MarketEvent(
            "GPIF関連 指数・ESGパッシブ確認", nth_weekday(year, month, 0, 1),
            "gpif_watch", "JP", "low",
            "GPIFの採用指数・ESG指数・公募ニュースを確認する四半期初の点検日"
            "（公式の直接リバランス日ではない）。",
            phase="watch",
        )
        for month in (1, 4, 7, 10)
    ]


# ---------------------------------------------------------------------------
# 集約
# ---------------------------------------------------------------------------

def _curated_events() -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for iso, name, category, region, importance, note in CURATED_EVENTS:
        try:
            d = datetime.strptime(iso, "%Y-%m-%d").date()
        except ValueError:
            continue
        events.append(MarketEvent(name, d, category, region, importance, note))
    return events


def _event_sort_key(event: MarketEvent) -> tuple:
    return (
        event.event_date,
        _PHASE_SORT.get(event.phase, 9),
        _IMPORTANCE_SORT.get(event.importance, 9),
        event.name,
    )


@lru_cache(maxsize=8)
def _events_for_year(year: int) -> tuple[MarketEvent, ...]:
    events: list[MarketEvent] = [e for e in _curated_events() if e.event_date.year == year]
    events.extend(_derivatives_events(year))
    events.extend(_dividend_events(year))
    events.extend(_ism_events(year))
    events.extend(_tankan_events(year))
    events.extend(_jp_ppi_events(year))
    events.extend(_msci_events(year))
    events.extend(_ftse_events(year))
    events.extend(_nikkei225_events(year))
    events.extend(_topix_events(year))
    events.extend(_jpx_nikkei400_events(year))
    events.extend(_jpx_prime150_events(year))
    events.extend(_gpif_watch_events(year))
    # 計算イベントが年をまたいで生成されることはないが、キュレーション由来の
    # 他年日付（例: 2027-01の日本CPI）が混ざらないよう年でフィルタする
    return tuple(sorted((e for e in events if e.event_date.year == year), key=_event_sort_key))


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
    candidates: list[MarketEvent] = []
    for year in range(start.year, end.year + 1):
        candidates.extend(_events_for_year(year))
    return sorted(
        (e for e in candidates if start <= e.event_date <= end), key=_event_sort_key
    )


def get_events_for_month(year: int, month: int) -> list[MarketEvent]:
    """指定月内の全イベントを日付順で返す。月間カレンダー表示用。"""
    return [e for e in _events_for_year(year) if e.event_date.month == month]


def build_event_calendar_prompt_block(target_date: str, max_lines: int = 30) -> str:
    """プロンプトへ注入する市場イベント・カレンダーのブロックを返す。

    low重要度（GPIFウォッチ・企業物価目安等）はノイズになるため除外する。
    """
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "【市場イベント・カレンダー】\n- 対象日の解釈に失敗。"

    events = [e for e in get_events_for_date(target_date) if e.importance != "low"]
    if len(events) > max_lines:
        highs = [e for e in events if e.importance == "high"]
        others = [e for e in events if e.importance != "high"]
        events = sorted(
            highs + others[: max(0, max_lines - len(highs))], key=_event_sort_key
        )

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
                f"[{e.region}/{e.phase_label()}] {e.name}: {e.note}"
            )

    lines.append(
        "- 解釈ルール: SQ週・先物ロール・指数リバランスの実売買日近辺では、"
        "その他(33業種外)や価格規制なし比率の上昇を機械的フローとして扱い、"
        "方向性売り（弱気）と断定しない。指数の結果発表日以降は採用・除外銘柄への"
        "先回りの空売り・買い戻しが増える。FOMC・日銀会合の直前はリスク回避の積み増し、"
        "通過後は巻き戻しが起きやすい。配当権利落ち日は配当再投資（先物買い）で需給が歪む。"
    )
    return "\n".join(lines)
