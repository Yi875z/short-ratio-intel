"""日次レポート生成のテスト（DB・ネットワーク非依存）。"""
import pandas as pd

from src.report.us_daily_report import build_daily_report


def _history(ticker: str, days: int = 25, base: float = 40.0) -> list[dict]:
    """Zスコアが算出できる程度の履歴を作る（分散ありで標準偏差0を避ける）。"""
    rows = []
    for i in range(days):
        ratio = base + (i % 3)
        total = 1_000_000.0
        rows.append({
            "date": f"2026-01-{i + 1:02d}",
            "ticker": ticker,
            "short_volume": ratio / 100 * total,
            "reported_total_volume": total,
            "short_ratio_pct": ratio,
        })
    return rows


def _sample_frames(spike_ratio: float = 60.0):
    rows = _history("NVDA") + _history("SMH", base=30.0)
    # 最終日に NVDA だけショート急増
    total = 1_000_000.0
    rows.append({
        "date": "2026-01-26", "ticker": "NVDA",
        "short_volume": spike_ratio / 100 * total,
        "reported_total_volume": total, "short_ratio_pct": spike_ratio,
    })
    rows.append({
        "date": "2026-01-26", "ticker": "SMH",
        "short_volume": 0.30 * total,
        "reported_total_volume": total, "short_ratio_pct": 30.0,
    })

    price = pd.DataFrame([
        {"date": "2026-01-26", "ticker": "NVDA", "high": 110, "low": 100, "close": 101, "market_volume": 5_000_000},
        {"date": "2026-01-26", "ticker": "SMH", "high": 110, "low": 100, "close": 109, "market_volume": 1_000_000},
    ])
    return pd.DataFrame(rows), price


def test_report_contains_required_sections():
    short_df, price_df = _sample_frames()

    report = build_daily_report("2026-01-26", short_df, price_df, universe=["NVDA", "SMH"])
    md = report["markdown"]

    for heading in ["データ健全性", "バスケット", "ETF乖離", "アラート", "パターン集計", "全銘柄", "注記"]:
        assert heading in md


def test_report_states_data_scope_limitations():
    """FINRA報告分であること・フローであることを毎回明示する。"""
    short_df, price_df = _sample_frames()

    md = build_daily_report("2026-01-26", short_df, price_df, universe=["NVDA", "SMH"])["markdown"]

    assert "Off-Exchange" in md
    assert "米国市場全体ではありません" in md
    assert "空売り残高（Short Interest）ではありません" in md


def test_report_avoids_definitive_language():
    """単日フローで断定しない（すべて候補として書く）。"""
    short_df, price_df = _sample_frames()

    md = build_daily_report("2026-01-26", short_df, price_df, universe=["NVDA", "SMH"])["markdown"]

    assert "候補" in md
    # 「断定しません」という免責文は正しいので、断定表現そのものを狙って検査する
    assert "単日のフローで方向性を断定しません" in md
    for forbidden in ["機関が売っている", "確実に", "間違いなく", "断定できる"]:
        assert forbidden not in md


def test_report_reports_missing_tickers():
    short_df, price_df = _sample_frames()

    report = build_daily_report(
        "2026-01-26", short_df, price_df, universe=["NVDA", "SMH", "AMD"]
    )

    assert report["coverage"]["present"] == 2
    assert report["coverage"]["expected"] == 3
    assert report["coverage"]["missing"] == ["AMD"]
    assert "AMD" in report["markdown"]


def test_report_handles_missing_data_without_crashing():
    """対象日のデータが無い日でもレポートを返す（例外にしない）。"""
    report = build_daily_report("2026-01-26", pd.DataFrame())

    assert report["alerts"] == []
    assert report["coverage"]["present"] == 0
    assert "データなし" in report["markdown"]
    assert report["highlights"]


def test_report_renders_unjudged_values_as_not_available():
    """Zスコアが出せない銘柄は数値をでっち上げず N/A と表示する。"""
    short_df = pd.DataFrame([{
        "date": "2026-01-26", "ticker": "NVDA",
        "short_volume": 400_000.0, "reported_total_volume": 1_000_000.0,
        "short_ratio_pct": 40.0,
    }])

    md = build_daily_report("2026-01-26", short_df, universe=["NVDA"])["markdown"]

    assert "N/A" in md
    assert "INSUFFICIENT_DATA" in md


def test_highlights_are_plain_text_for_slack():
    short_df, price_df = _sample_frames()

    highlights = build_daily_report(
        "2026-01-26", short_df, price_df, universe=["NVDA", "SMH"]
    )["highlights"]

    assert "米国ショートフロー" in highlights
    assert "|" not in highlights          # Markdownテーブルを流し込まない
    assert len(highlights.splitlines()) <= 12
