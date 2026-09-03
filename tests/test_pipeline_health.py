"""
パイプライン自己点検のテスト（ネットワーク・DB非依存）。

このモジュールは「静かに落ちる宿題」を鳴らすためにある。
2026-07-03 の引き継ぎメモにあった「H2に日本GDPの公表日を追記」が
2026-09-01 時点で未着手のままカレンダーに穴を空けていた実例が出発点。
"""
from datetime import date

import pytest

from src.macro_context.pipeline_health import (
    HealthIssue,
    check_breakdown_gaps,
    check_calendar_coverage,
    check_data_freshness,
    check_validation_staleness,
    format_health_block,
    has_blocking_issues,
)


# ------------------------------------------------------------------
# カレンダーの穴
# ------------------------------------------------------------------
def test_登録が尽きたカテゴリを検出する(monkeypatch):
    """公表日が追記されず先が尽きているカテゴリを鳴らす。"""
    import config.market_calendar as calendar

    monkeypatch.setattr(calendar, "CURATED_EVENTS", [
        ("2026-05-19", "日本GDP 1次速報", "gdp", "JP", "medium", ""),
        ("2026-12-01", "米GDP", "gdp", "US", "medium", ""),
    ])

    issues = check_calendar_coverage(today=date(2026, 9, 1), horizon_days=60)

    assert len(issues) == 1
    assert "JP/gdp" in issues[0].message
    assert issues[0].severity == "high"      # 既に過去なので high
    assert "market_calendar.py" in issues[0].action


def test_先の日程が十分あれば鳴らさない(monkeypatch):
    import config.market_calendar as calendar

    monkeypatch.setattr(calendar, "CURATED_EVENTS", [
        ("2026-12-01", "日本GDP", "gdp", "JP", "medium", ""),
    ])
    assert check_calendar_coverage(today=date(2026, 9, 1), horizon_days=60) == []


def test_尽きかけは中程度として鳴らす(monkeypatch):
    """まだ未来だが期限内に尽きる場合は medium。"""
    import config.market_calendar as calendar

    monkeypatch.setattr(calendar, "CURATED_EVENTS", [
        ("2026-09-20", "日本GDP", "gdp", "JP", "medium", ""),
    ])
    issues = check_calendar_coverage(today=date(2026, 9, 1), horizon_days=60)

    assert len(issues) == 1
    assert issues[0].severity == "medium"
    assert "残り19日" in issues[0].message


# ------------------------------------------------------------------
# データ鮮度
# ------------------------------------------------------------------
def test_保存が止まっているデータを検出する():
    issues = check_data_freshness(
        {"空売り比率": "2026-08-20", "騰落銘柄数": "2026-08-31"},
        today=date(2026, 9, 1),
    )
    assert len(issues) == 1
    assert "空売り比率" in issues[0].message
    assert "12日前" in issues[0].message


def test_一件も保存が無い場合も検出する():
    issues = check_data_freshness({"業種別フロー特徴量": None}, today=date(2026, 9, 1))
    assert len(issues) == 1
    assert "1件も保存されていません" in issues[0].message


def test_新しいデータは鳴らさない():
    assert check_data_freshness(
        {"空売り比率": "2026-08-31"}, today=date(2026, 9, 1)
    ) == []


# ------------------------------------------------------------------
# JPX内訳の欠落（当日中に気づかないと永久に失われる）
# ------------------------------------------------------------------
def _market_row(date, ratio, total_short):
    return {"date": date, "short_ratio_pct": ratio, "total_short_va": total_short}


def test_JPXがまだ公開している日の欠測はcriticalになる():
    """今日取り直せば埋まる。逃すと当月中は取れない（アーカイブ入りは翌月）。"""
    rows = [
        _market_row("2026-08-25", 43.05, 3_320_455),
        _market_row("2026-08-26", 42.70, 0),
        _market_row("2026-08-27", 45.00, 0),
    ]
    issues = check_breakdown_gaps(rows)

    assert len(issues) == 1
    assert issues[0].severity == "critical"
    assert issues[0].blocking is True
    assert "2026-08-26 / 2026-08-27" in issues[0].message
    assert "backfill_jpx_breakdown" in issues[0].action


def test_一覧から落ちた日の欠測はcriticalにしない():
    """月別アーカイブから取り直せるので締切が無い。毎日鳴らすと無視される。"""
    rows = [
        _market_row("2026-08-25", 43.05, 0),      # 一覧から落ちている
        _market_row("2026-08-26", 42.70, 3_100_000),
        _market_row("2026-08-27", 45.00, 3_900_000),
    ]
    issues = check_breakdown_gaps(rows)

    assert len(issues) == 1
    assert issues[0].severity == "high"
    assert issues[0].blocking is False
    assert "アーカイブ" in issues[0].action


def test_締切ありと締切なしを別々に鳴らす():
    rows = [
        _market_row("2026-08-24", 43.0, 0),       # 古い欠測
        _market_row("2026-08-25", 43.0, 3_000_000),
        _market_row("2026-08-26", 42.7, 3_100_000),
        _market_row("2026-08-27", 45.0, 0),       # 一覧に載っている欠測
    ]
    issues = check_breakdown_gaps(rows)

    assert [i.severity for i in issues] == ["critical", "high"] or            sorted(i.severity for i in issues) == ["critical", "high"]
    assert has_blocking_issues(issues) is True


def test_内訳が規制ありだけ入っている日も欠測として数えない():
    """部分的にでも内訳が入っていれば、スクレイパー由来の0ではない。"""
    rows = [{
        "date": "2026-08-27", "short_ratio_pct": 45.0,
        "total_short_va": 0, "shrt_with_res_va": 3_000_000, "shrt_no_res_va": 0,
    }]
    assert check_breakdown_gaps(rows) == []


def test_締切なしの欠測だけならパイプラインを落とさない():
    rows = [
        _market_row("2026-08-25", 43.0, 0),
        _market_row("2026-08-26", 42.7, 3_100_000),
        _market_row("2026-08-27", 45.0, 3_900_000),
    ]
    assert has_blocking_issues(check_breakdown_gaps(rows)) is False


def test_内訳が揃っていれば鳴らさない():
    rows = [_market_row(f"2026-08-{i:02d}", 43.0, 3_000_000) for i in range(20, 28)]
    assert check_breakdown_gaps(rows) == []


def test_直近窓の外の欠落は鳴らさない():
    """点検窓より前は日次通知の担当ではない（復旧はバックフィルの担当）。"""
    rows = [_market_row("2026-04-20", 43.0, 0)]
    rows += [_market_row(f"2026-08-{i:02d}", 43.0, 3_000_000) for i in range(18, 29)]
    assert check_breakdown_gaps(rows, recent_days=5) == []


def test_比率も無い日は内訳欠落として数えない():
    """そもそも取得できていない日は鮮度チェックの担当。"""
    rows = [_market_row("2026-08-26", 0, 0)]
    assert check_breakdown_gaps(rows) == []


# ------------------------------------------------------------------
# 検証レポートの鮮度（半年後の宿題を機械に持たせる部分）
# ------------------------------------------------------------------
def test_検証レポートが古ければ鳴らす():
    issues = check_validation_staleness("2026-01-01", today=date(2026, 9, 1))
    assert len(issues) == 1
    assert "検証レポート" in issues[0].message
    assert "validate_sector_features" in issues[0].action


def test_検証レポートが新しければ鳴らさない():
    assert check_validation_staleness("2026-08-01", today=date(2026, 9, 1)) == []


def test_検証レポートが未生成なら鳴らす():
    issues = check_validation_staleness(None, today=date(2026, 9, 1))
    assert len(issues) == 1
    assert "未生成" in issues[0].message


# ------------------------------------------------------------------
# 通知の整形
# ------------------------------------------------------------------
def test_問題が無ければ空文字を返す():
    """正常時に通知を汚さない。鳴りっぱなしだと読まれなくなる。"""
    assert format_health_block([]) == ""


def test_重大なものから並べて出す():
    issues = [
        HealthIssue("medium", "検証", "レポートが古い"),
        HealthIssue("high", "データ鮮度", "取得が止まっている"),
    ]
    block = format_health_block(sorted(issues, key=lambda i: i.severity))

    assert "自己点検: 2件" in block
    assert block.index("🔴") < block.index("🟡")


def test_件数が多いときは打ち切って残数を示す():
    issues = [HealthIssue("medium", "検証", f"項目{i}") for i in range(12)]
    block = format_health_block(issues, limit=3)

    assert "自己点検: 12件" in block
    assert "ほか9件" in block
