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

# 市場売買代金に占めるシェアがこれ未満の業種は「薄商い業種」として扱う（%）。
# 33業種の平均は約3%。鉱業などは0.1〜0.3%しかなく、少額の売買で比率が10pt以上動く。
# 2026-09-30 のレポートは鉱業の -18.9pt を「壊滅的下落」と書いていた。
# プロは比率の変化を売買代金で重みづけて読む。単日の大変化は、厚い業種でなければ信号にしない。
THIN_SECTOR_SHARE_PCT: float = 0.5

# 業種履歴を読む暦日数。窓60営業日を満たせる長さ。画面・AIプロンプト・異常値検知で共通に使う
# （2026-09-30 まで AIと異常値検知だけ14日で、画面の90日と同じ業種のZが食い違っていた）。
SECTOR_HISTORY_DAYS = 90

# 判定に必要な最低サンプル数（営業日）。2026-09-30 に 5 → 20 へ引き上げた。
# 5件のZスコアを「過去60日のXパーセンタイル」と出していたが、5点の標準偏差は不安定で、
# ナレッジ29 §7（履歴が浅い期間では使わない）とも合わなかった。実際の件数は行に併記する（n）。
# 米国側は窓幅を満たすことを要求するが、業種別空売りは休場・欠測で履歴が浅い日があるため、
# 窓に対する比率ではなく「最低件数」として扱う。
_MIN_HISTORY_SAMPLES = 20
_MIN_COVERAGE = _MIN_HISTORY_SAMPLES / _ZSCORE_WINDOW

# 自己比Zの「極値」とみなす絶対値（異常値検知の ANOMALY_ZSCORE_THRESHOLD と同じ 2.0）
_Z_EXTREME = 2.0


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


def self_zscore(history_df, s33_code, current, target_date) -> tuple[Optional[float], Optional[float], int]:
    """その業種自身の過去（当日を除く・直近60営業日）に対する Zスコア・パーセンタイル・件数。

    画面・AIプロンプト・異常値検知のすべてがこの1つを使う（同じ業種に別々のZを出さない）。
    件数が _MIN_HISTORY_SAMPLES 未満なら (None, None, 件数)。
    """
    past = _past_values(history_df, s33_code, target_date)
    n = len([v for v in past[-_ZSCORE_WINDOW:] if v is not None and v == v])
    if n < _MIN_HISTORY_SAMPLES:
        return None, None, n
    return (
        zscore(past, current, _ZSCORE_WINDOW, _MIN_COVERAGE),
        percentile_rank(past, current, _ZSCORE_WINDOW, _MIN_COVERAGE),
        n,
    )


def count_z_extreme_streak(history_df, s33_code, target_date, max_days: int = 10) -> int:
    """自己比Zが同じ向きに ±2 を超えた状態が、最新日から何営業日続いているか。

    ナレッジ29 §5 の「自己比Zの極値が2営業日以上続く」を数える。固定ゾーン（47%以上）の
    連続日数（count_zone_streak）は業種ごとの平常水準の違いを無視するので、こちらを信号側に使う。
    各日のZは、その日より前の履歴だけで計算する（先読みしない）。
    """
    if history_df is None or len(history_df) == 0 or "s33_code" not in history_df.columns:
        return 0
    rows = history_df[history_df["s33_code"] == s33_code].sort_values("date")
    rows = rows[rows["date"].map(_as_day) <= _as_day(target_date)] if target_date else rows
    days = [(_as_day(d), v) for d, v in zip(rows["date"], rows["short_ratio_pct"])]

    streak, sign = 0, 0
    for day, value in reversed(days[-max_days:]):
        z, _, _ = self_zscore(history_df, s33_code, value, day)
        if z is None or abs(z) < _Z_EXTREME:
            break
        current_sign = 1 if z > 0 else -1
        if sign and current_sign != sign:
            break
        sign = current_sign
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

    # 売買代金シェアの分母。市場全体（JPX公式・33業種外を含む）を優先し、無ければ業種の合計。
    market_volume = (today_summary.get("market_breakdown") or {}).get("total_volume_va") or sum(
        (s.get("total_volume_va") or 0) for s in today_summary.get("sector_data", [])
    )

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

        z_value, pct_value, sample_count = self_zscore(history_df, s33_code, current, target_date)
        # 内訳が無い日（スクレイパー経路）は規制あり/なし・売買代金を「未取得」として扱う。
        # 0 のまま計算すると「規制あり0.0%」「売買代金シェア0.0%（薄商い）」と事実に反する行になる。
        has_breakdown = bool(short_with or short_without)

        rows.append({
            "sector_name": s.get("sector_name"),
            "s33_code": s33_code,
            "short_ratio_pct": current,
            "dod_change": dod,
            "zone_label": s.get("zone_label"),
            "zone_key": s.get("zone_key"),
            "change_pct": change_pct,
            "quadrant": format_quadrant(dod, change_pct),
            "zscore": z_value,
            "percentile": pct_value,
            "zscore_samples": sample_count,
            "z_extreme_streak": count_z_extreme_streak(history_df, s33_code, target_date),
            "has_breakdown": has_breakdown,
            "with_ratio": _ratio(short_with, total_volume) if has_breakdown else None,
            "without_ratio": _ratio(short_without, total_volume) if has_breakdown else None,
            "without_share": _ratio(short_without, total_short) if has_breakdown else None,
            "streak_days": count_zone_streak(history_df, s33_code),
            "volume_share": (
                _ratio(total_volume, market_volume) if market_volume and total_volume else None
            ),
        })

    return rows


def format_sector_prompt_line(row: dict) -> str:
    """AIプロンプト用の業種1行。

    2026-09-30 に「自己比」と「厚み」を足した。それまでAIは固定の絶対水準ゾーン
    （43〜47% 等）しか見ておらず、業種ごとに構造的に違う水準を横並びで読んでいた。
    Zスコア・パーセンタイル・連続日数は画面では出していたのにAIへ渡していなかった。
    """
    dod = row.get("dod_change")
    dod_str = f"{dod:+.1f}pt" if dod is not None else "N/A"
    change_pct = row.get("change_pct")
    price_str = f"株価{change_pct:+.2f}%" if change_pct is not None else "株価N/A"
    quadrant = row.get("quadrant") or ""

    if row.get("has_breakdown", True) and row.get("with_ratio") is not None:
        breakdown_str = (
            f"規制あり{row['with_ratio']:4.1f}% / 規制なし{row['without_ratio']:4.1f}% "
            f"(規制なし構成比{row['without_share']:4.1f}%)"
        )
    else:
        # 0.0% と書くと「規制ありの空売りがゼロだった」と読まれる（PROJECT_RULES: 0と書かず未取得と書く）
        breakdown_str = "規制あり/なし内訳: 未取得"

    line = (
        f"{row['sector_name']:20s}: 総空売り{row['short_ratio_pct']:5.1f}% ({dod_str}) / "
        f"{price_str} / {breakdown_str} / {row['zone_label']}"
        + (f" / {quadrant}" if quadrant else "")
    )

    zscore_value = row.get("zscore")
    percentile = row.get("percentile")
    samples = row.get("zscore_samples")
    if zscore_value is not None:
        line += f" / 自己比Z{zscore_value:+.1f}"
        if percentile is not None:
            line += f"（直近{samples}営業日の{percentile:.0f}パーセンタイル）"
        z_streak = row.get("z_extreme_streak") or 0
        if z_streak >= 2:
            line += f" / 自己比Z±2超が{z_streak}日連続"
    else:
        line += f" / 自己比Z N/A（履歴{samples or 0}営業日で不足）"

    streak = row.get("streak_days") or 0
    if streak >= 2:
        line += f" / 警戒ゾーン（固定水準）{streak}日連続"

    share = row.get("volume_share")
    if share is not None:
        line += f" / 売買代金シェア{share:.1f}%"
        if share < THIN_SECTOR_SHARE_PCT:
            line += "（薄商い業種: 単日の比率変化はノイズとして扱う）"
    else:
        line += " / 売買代金シェア: 未取得"
    return line
