"""
騰落銘柄数テーブルの保存ロジックテスト（一時SQLite・ネットワーク非依存）。

空売り比率の既存テーブルに一切触れないことも併せて確認する。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.analyzer.market_breadth import BreadthCounts
from src.storage import db
from src.storage.models import (
    Base,
    MarketBreadthDaily,
    MarketShortRatioDaily,
    ShortRatioDaily,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """本番DBに触らない一時SQLiteへ差し替える。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    return engine


def _counts(date="2026-08-28", scope="TSE_PRIME", advancing=873, declining=635):
    return BreadthCounts(
        date=date,
        scope=scope,
        scope_label="プライム",
        advancing=advancing,
        declining=declining,
        unchanged=49,
        not_compared=0,
        universe=1557,
    )


def _record(topix=True, **kwargs):
    record = _counts(**kwargs).to_dict()
    if topix:
        record["topix_close"] = 4146.71
        record["topix_prev_close"] = 4117.22
        record["topix_change_pct"] = 0.716
    return record


# ------------------------------------------------------------------
# 保存
# ------------------------------------------------------------------
def test_騰落銘柄数を保存できる(temp_db):
    saved = db.upsert_market_breadth_records([_record()])

    assert saved == 1
    with Session(temp_db) as session:
        row = session.query(MarketBreadthDaily).one()
    assert row.date == "2026-08-28"
    assert row.market_scope == "TSE_PRIME"
    assert row.scope_label == "プライム"
    assert row.advancing_issues == 873
    assert row.declining_issues == 635
    assert row.unchanged_issues == 49
    assert row.universe_issues == 1557
    assert row.topix_change_pct == pytest.approx(0.716)
    assert row.source == "JQUANTS_V2"


def test_同じ日付とスコープの再投入で行が増えない(temp_db):
    """途中で失敗しても同じコマンドを再実行できる（冪等）。"""
    db.upsert_market_breadth_records([_record()])
    db.upsert_market_breadth_records([_record(advancing=900)])

    with Session(temp_db) as session:
        rows = session.query(MarketBreadthDaily).all()
    assert len(rows) == 1
    assert rows[0].advancing_issues == 900  # 後勝ちで更新される


def test_同じ日でもスコープが違えば別の行になる(temp_db):
    db.upsert_market_breadth_records([
        _record(scope="TSE_PRIME"),
        _record(scope="TSE_STANDARD"),
        _record(scope="TSE_GROWTH"),
    ])

    with Session(temp_db) as session:
        rows = session.query(MarketBreadthDaily).all()
    assert len(rows) == 3
    assert {r.market_scope for r in rows} == {"TSE_PRIME", "TSE_STANDARD", "TSE_GROWTH"}


def test_TOPIXが取れない日はNoneのまま保存し補間しない(temp_db):
    db.upsert_market_breadth_records([_record(topix=False)])

    with Session(temp_db) as session:
        row = session.query(MarketBreadthDaily).one()
    assert row.topix_close is None
    assert row.topix_change_pct is None
    assert row.advancing_issues == 873  # 騰落銘柄数は保存される


def test_必須項目が欠けたレコードは黙って捨てる(temp_db):
    broken = _record()
    del broken["scope"]

    saved = db.upsert_market_breadth_records([broken, _record()])

    assert saved == 1
    with Session(temp_db) as session:
        assert len(session.query(MarketBreadthDaily).all()) == 1


def test_空リストは何もしない(temp_db):
    assert db.upsert_market_breadth_records([]) == 0


# ------------------------------------------------------------------
# 読み出し
# ------------------------------------------------------------------
def test_期間とスコープで絞って読み出せる(temp_db):
    db.upsert_market_breadth_records([
        _record(date="2026-08-26", scope="TSE_PRIME"),
        _record(date="2026-08-27", scope="TSE_PRIME"),
        _record(date="2026-08-28", scope="TSE_PRIME"),
        _record(date="2026-08-28", scope="TSE_GROWTH"),
    ])

    df = db.get_market_breadth_df(
        from_date="2026-08-27", to_date="2026-08-28", market_scope="TSE_PRIME"
    )

    assert list(df["date"]) == ["2026-08-27", "2026-08-28"]
    assert set(df["market_scope"]) == {"TSE_PRIME"}


def test_保存済み日付を新しい順に返す(temp_db):
    db.upsert_market_breadth_records([
        _record(date="2026-08-26"),
        _record(date="2026-08-28"),
        _record(date="2026-08-27"),
    ])

    assert db.get_saved_market_breadth_dates() == [
        "2026-08-28", "2026-08-27", "2026-08-26",
    ]
    assert db.get_market_breadth_latest_date() == "2026-08-28"


def test_データが無ければ空のDataFrameとNoneを返す(temp_db):
    assert db.get_market_breadth_df().empty
    assert db.get_saved_market_breadth_dates() == []
    assert db.get_market_breadth_latest_date() is None


# ------------------------------------------------------------------
# 既存テーブルへの非干渉
# ------------------------------------------------------------------
def test_騰落銘柄数の保存は空売り比率テーブルに触れない(temp_db):
    """需給モニターの追加で既存の空売り比率データが壊れないことを固定する。"""
    db.upsert_short_ratio_records([{
        "Date": "2026-08-28", "S33": "3650", "SectorName": "電気機器",
        "ShortRatioPct": 44.1, "TotalVolumeVa": 1000.0, "TotalShortVa": 441.0,
    }])
    db.upsert_market_breadth_records([_record()])

    with Session(temp_db) as session:
        assert len(session.query(ShortRatioDaily).all()) == 1
        assert len(session.query(MarketBreadthDaily).all()) == 1
        assert len(session.query(MarketShortRatioDaily).all()) == 0
        assert session.query(ShortRatioDaily).one().short_ratio_pct == 44.1
