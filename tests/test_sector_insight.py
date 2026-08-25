"""
業種別空売りの「文脈」（株価騰落率・4象限・Zスコア・規制内訳・連続日数）の決定論テスト。

この計算はもともと prompt_builder の中にインラインで埋まっており、AIプロンプトの
文字列としてしか存在しなかった。切り出しても **AIへ渡る行が1文字も変わらない**ことを
回帰テストで固定する（レポート品質を改修の巻き添えで動かさないため）。
"""
import pandas as pd

from src.analyzer.sector_insight import (
    HIGH_ZONE_MIN_RATIO,
    build_sector_insights,
    count_zone_streak,
    format_sector_prompt_line,
)


def _sector(s33_code, name, ratio, dod, zone_label, with_va=300, no_va=200, volume=1000):
    return {
        "s33_code": s33_code,
        "sector_name": name,
        "short_ratio_pct": ratio,
        "dod_change": dod,
        "zone_key": "high_alert",
        "zone_label": zone_label,
        "shrt_with_res_va": with_va,
        "shrt_no_res_va": no_va,
        "total_short_va": with_va + no_va,
        "total_volume_va": volume,
    }


def _today_summary():
    return {
        "date": "2026-08-24",
        "sector_data": [
            _sector("3650", "電気機器", 48.0, 2.5, "🟠 警戒ゾーン（47〜50%）"),
            _sector("6050", "サービス業", 40.0, -1.0, "🔵 正常レンジ（37〜43%）"),
        ],
    }


def _history(code="3650", ratios=None, end="2026-08-24"):
    """指定業種の履歴。最後の1件が当日（end）になるよう日付を振る。"""
    ratios = ratios if ratios is not None else [45.0, 46.0, 47.5, 48.5, 49.0, 47.2, 48.0]
    dates = pd.date_range(end=end, periods=len(ratios), freq="D")
    return pd.DataFrame({
        "date": dates,
        "s33_code": [code] * len(ratios),
        "sector_name": ["電気機器"] * len(ratios),
        "short_ratio_pct": ratios,
    })


# ──────────────────────────────────────────────────────────────
# 文脈の組み立て
# ──────────────────────────────────────────────────────────────
def test_quadrant_and_restriction_ratios():
    returns = {"3650": {"change_pct": -1.2}, "6050": {"change_pct": 0.8}}

    rows = build_sector_insights(_today_summary(), _history(), returns)
    electric = rows[0]

    assert electric["quadrant"] == "比率上昇×株価下落=方向性売り優勢の可能性"
    assert electric["with_ratio"] == 30.0        # 300 / 1000
    assert electric["without_ratio"] == 20.0     # 200 / 1000
    assert electric["without_share"] == 40.0     # 200 / 500
    assert electric["change_pct"] == -1.2


def test_missing_price_leaves_quadrant_empty_without_raising():
    """業種別株価が取れない日でも落ちない（比率だけで読ませる）。"""
    rows = build_sector_insights(_today_summary(), _history(), {})

    assert rows[0]["quadrant"] == ""
    assert rows[0]["change_pct"] is None


def test_zscore_needs_enough_history():
    """履歴5営業日未満は判定しない（AnomalyDetector._calc_zscore と同じ基準）。"""
    short_history = _history(ratios=[46.0, 47.0, 48.0])

    rows = build_sector_insights(_today_summary(), short_history, {})

    assert rows[0]["zscore"] is None
    assert rows[0]["percentile"] is None


def test_zscore_is_computed_from_the_sectors_own_distribution():
    rows = build_sector_insights(_today_summary(), _history(), {})

    assert rows[0]["zscore"] is not None
    assert 0.0 <= rows[0]["percentile"] <= 100.0
    # 履歴を持たない業種は判定しない
    assert rows[1]["zscore"] is None


def test_works_without_history_at_all():
    rows = build_sector_insights(_today_summary(), None, None)

    assert rows[0]["zscore"] is None
    assert rows[0]["streak_days"] == 0
    assert rows[0]["quadrant"] == ""


# ──────────────────────────────────────────────────────────────
# 連続日数
# ──────────────────────────────────────────────────────────────
def test_streak_counts_back_from_the_latest_day():
    history = _history(ratios=[40.0, 48.0, 49.0, 47.5])

    assert count_zone_streak(history, "3650") == 3


def test_streak_breaks_on_the_first_low_day():
    history = _history(ratios=[49.0, 49.0, 40.0, 48.0])

    assert count_zone_streak(history, "3650") == 1


def test_streak_counts_the_whole_history_when_always_high():
    history = _history(ratios=[48.0, 48.5, 49.0])

    assert count_zone_streak(history, "3650") == 3


def test_streak_is_zero_without_history():
    assert count_zone_streak(None, "3650") == 0
    assert count_zone_streak(_history(), "9999") == 0


def test_high_zone_threshold_comes_from_the_zone_table():
    """47.0 をコードに直書きしていないこと（ゾーン定義が正）。"""
    assert HIGH_ZONE_MIN_RATIO == 47.0


# ──────────────────────────────────────────────────────────────
# 回帰: AIプロンプトへ渡る行を変えない
# ──────────────────────────────────────────────────────────────
def test_prompt_line_matches_the_previous_format():
    rows = build_sector_insights(_today_summary(), _history(), {"3650": {"change_pct": -1.2}})

    expected = (
        "電気機器" + " " * 16
        + ": 総空売り 48.0% (+2.5pt) / 株価-1.20% / "
        + "規制あり30.0% / 規制なし20.0% (規制なし構成比40.0%) / 🟠 警戒ゾーン（47〜50%）"
        + " / 比率上昇×株価下落=方向性売り優勢の可能性"
    )
    assert format_sector_prompt_line(rows[0]) == expected


def test_prompt_line_without_price_or_dod():
    summary = {
        "date": "2026-08-24",
        "sector_data": [_sector("3650", "電気機器", 48.0, None, "🟠 警戒ゾーン（47〜50%）")],
    }

    line = format_sector_prompt_line(build_sector_insights(summary, None, {})[0])

    assert "(N/A)" in line
    assert "株価N/A" in line
    assert line.endswith("🟠 警戒ゾーン（47〜50%）")
