"""
業種別空売り比率に「文脈」を付ける（株価騰落率・4象限・Zスコア・規制内訳・連続日数）。

規制なし構成比・株価騰落率・4象限は、これまで prompt_builder の中でインラインに計算され、
AIプロンプトの文字列としてしか存在しなかった。画面からは見えないため、4象限を知るには
AIレポートを読むしかない状態だった。ここへ切り出して、AI と画面が同じ計算を使う。

業種は構造的に空売り比率の水準が違う（証券業は元から高く、電気・ガス業は低い）。
生の 45.8% を横並びで比べても意味が薄いので、その業種自身の過去分布に対する
Zスコア／パーセンタイルを併せて出す。米国側（us_flow_analyzer）と同じ思想。
"""
from __future__ import annotations

from typing import Optional

from config.sectors import SECTOR_ZONES
from src.analyzer.us_flow_analyzer import percentile_rank, zscore
from src.macro_context.sector_price import format_quadrant

# 「高空売り」の定義はゾーン表を正とする（47.0 をここに直書きしない）
HIGH_ZONE_MIN_RATIO: float = SECTOR_ZONES["high_alert"]["min"]

# Zスコア／パーセンタイルの窓幅（営業日）。
_ZSCORE_WINDOW = 60

# 判定に必要な最低サンプル数。AnomalyDetector._calc_zscore と同じ 5 件に揃える。
# 米国側は窓幅を満たすことを要求するが、業種別空売りは休場・欠測で履歴が浅い日があるため、
# 窓に対する比率ではなく「最低件数」として扱う。
_MIN_HISTORY_SAMPLES = 5
_MIN_COVERAGE = _MIN_HISTORY_SAMPLES / _ZSCORE_WINDOW


def _as_day(value) -> str:
    """日付らしきものを 'YYYY-MM-DD' の文字列へ揃える（str/Timestamp どちらでも動く）。"""
    return str(value)[:10]


def _sector_series(history_df, s33_code) -> list:
    """指定業種の空売り比率を古い順のリストで返す。履歴が無ければ空リスト。"""
    if history_df is None or len(history_df) == 0:
        return []
    if "s33_code" not in history_df.columns:
        return []

    rows = history_df[history_df["s33_code"] == s33_code]
    if rows.empty:
        return []
    return rows.sort_values("date")["short_ratio_pct"].tolist()


def _past_values(history_df, s33_code, target_date) -> list:
    """当日を除いた過去の空売り比率（古い順）。Zスコアは当日を母集団に含めない。"""
    if history_df is None or len(history_df) == 0:
        return []
    if "s33_code" not in history_df.columns:
        return []

    rows = history_df[history_df["s33_code"] == s33_code]
    if rows.empty:
        return []

    rows = rows.sort_values("date")
    if target_date:
        day = _as_day(target_date)
        rows = rows[rows["date"].map(_as_day) < day]
    return rows["short_ratio_pct"].tolist()


def count_zone_streak(history_df, s33_code, min_ratio: float = HIGH_ZONE_MIN_RATIO) -> int:
    """最新日から遡って連続で min_ratio 以上だった営業日数を返す。

    単日 50% より「5営業日連続で警戒ゾーン」の方が踏み上げの燃料としては重い。
    単日スパイクと持続的な売り圧を分けるための指標。
    """
    streak = 0
    for value in reversed(_sector_series(history_df, s33_code)):
        if value is None or value != value:   # None / NaN で打ち切り
            break
        if float(value) < min_ratio:
            break
        streak += 1
    return streak


def _ratio(numerator, denominator) -> float:
    return numerator / denominator * 100 if denominator else 0.0


def build_sector_insights(
    today_summary: dict,
    history_df=None,
    sector_returns: Optional[dict] = None,
) -> list[dict]:
    """業種ごとに空売り比率＋文脈を1行の dict にまとめて返す。

    Args:
        today_summary:   RatioCalculator.get_today_summary() の結果
        history_df:      過去N日の全業種データ（Zスコア・連続日数用。無くても動く）
        sector_returns:  returns_by_sector_code() の結果（S33コード→騰落率。無くても動く）
    """
    sector_returns = sector_returns or {}
    target_date = today_summary.get("date")
    rows: list[dict] = []

    for s in today_summary.get("sector_data", []):
        s33_code = s.get("s33_code")
        dod = s.get("dod_change")
        current = s.get("short_ratio_pct")

        total_volume = s.get("total_volume_va", 0) or 0
        short_with = s.get("shrt_with_res_va", 0) or 0
        short_without = s.get("shrt_no_res_va", 0) or 0
        total_short = s.get("total_short_va", short_with + short_without) or 0

        price = sector_returns.get(s33_code)
        change_pct = price.get("change_pct") if price else None

        past = _past_values(history_df, s33_code, target_date)
        has_enough_history = len(past) >= _MIN_HISTORY_SAMPLES

        rows.append({
            "sector_name": s.get("sector_name"),
            "s33_code": s33_code,
            "short_ratio_pct": current,
            "dod_change": dod,
            "zone_label": s.get("zone_label"),
            "zone_key": s.get("zone_key"),
            "change_pct": change_pct,
            "quadrant": format_quadrant(dod, change_pct),
            "zscore": (
                zscore(past, current, _ZSCORE_WINDOW, _MIN_COVERAGE)
                if has_enough_history else None
            ),
            "percentile": (
                percentile_rank(past, current, _ZSCORE_WINDOW, _MIN_COVERAGE)
                if has_enough_history else None
            ),
            "with_ratio": _ratio(short_with, total_volume),
            "without_ratio": _ratio(short_without, total_volume),
            "without_share": _ratio(short_without, total_short),
            "streak_days": count_zone_streak(history_df, s33_code),
        })

    return rows


def format_sector_prompt_line(row: dict) -> str:
    """AIプロンプト用の業種1行。表記は従来のままに保つ（レポート品質を動かさないため）。"""
    dod = row.get("dod_change")
    dod_str = f"{dod:+.1f}pt" if dod is not None else "N/A"
    change_pct = row.get("change_pct")
    price_str = f"株価{change_pct:+.2f}%" if change_pct is not None else "株価N/A"
    quadrant = row.get("quadrant") or ""

    return (
        f"{row['sector_name']:20s}: 総空売り{row['short_ratio_pct']:5.1f}% ({dod_str}) / "
        f"{price_str} / "
        f"規制あり{row['with_ratio']:4.1f}% / 規制なし{row['without_ratio']:4.1f}% "
        f"(規制なし構成比{row['without_share']:4.1f}%) / {row['zone_label']}"
        + (f" / {quadrant}" if quadrant else "")
    )
