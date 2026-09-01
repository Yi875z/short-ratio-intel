"""
業種別フロー特徴量テーブルの保存テスト（一時SQLite・ネットワーク非依存）。

特に「特徴量の取り直しが、別パスで埋めた将来リターンを消さない」ことを固定する。
ここが壊れるとバックフィルのたびに検証データが失われ、静かに再現不能になる。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.analyzer.sector_flow_features import SectorFlowFeatures
from src.storage import db
from src.storage.models import Base, SectorFlowFeatureDaily, ShortRatioDaily


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    return engine


def _record(date="2026-08-28", s33="3650", **overrides):
    base = SectorFlowFeatures(
        date=date, s33_code=s33,
        constituents=240, compared=238,
        ret_cap_weighted=1.25, ret_equal_weighted=0.98, excess_ret_vs_topix=0.53,
        above_vwap_pct=61.3, high_close_pct=42.0, advancing_pct=58.4,
        close_above_open_pct=55.1, close_location_median=0.62,
        turnover_total=1_234_567_890.0, top_n=10, top_n_turnover_share=71.8,
        top_n_above_vwap=8, top_n_high_close=7, top_n_advancing=8,
        top_n_codes=("68570", "69200", "80350"),
    ).to_dict()
    base.update(overrides)
    return base


def test_特徴量を保存できる(temp_db):
    saved = db.upsert_sector_flow_features([_record()])

    assert saved == 1
    with Session(temp_db) as session:
        row = session.query(SectorFlowFeatureDaily).one()
    assert row.s33_code == "3650"
    assert row.above_vwap_pct == pytest.approx(61.3)
    assert row.top_n_turnover_share == pytest.approx(71.8)
    assert row.constituents == 240
    assert row.compared == 238


def test_上位銘柄コードはJSONで往復できる(temp_db):
    db.upsert_sector_flow_features([_record()])
    df = db.get_sector_flow_features_df(date="2026-08-28")
    assert df.iloc[0]["top_n_codes"] == ["68570", "69200", "80350"]


def test_同じ日付と業種の再投入で行が増えない(temp_db):
    db.upsert_sector_flow_features([_record()])
    db.upsert_sector_flow_features([_record(above_vwap_pct=70.0)])

    with Session(temp_db) as session:
        rows = session.query(SectorFlowFeatureDaily).all()
    assert len(rows) == 1
    assert rows[0].above_vwap_pct == pytest.approx(70.0)


def test_特徴量の取り直しは将来リターンを消さない(temp_db):
    """ここが本テストの主眼。

    バックフィルを流し直すたびに fwd_* が None で上書きされると、
    検証データが静かに失われて再現不能になる。
    """
    db.upsert_sector_flow_features([_record()])
    db.update_sector_forward_returns({
        "2026-08-28|3650": {"fwd_ret_1d": 1.5, "fwd_excess_1d": 0.7},
    })

    # 特徴量だけを取り直す（fwd_* を含まないレコード）
    db.upsert_sector_flow_features([_record(above_vwap_pct=65.0)])

    with Session(temp_db) as session:
        row = session.query(SectorFlowFeatureDaily).one()
    assert row.above_vwap_pct == pytest.approx(65.0)   # 特徴量は更新される
    assert row.fwd_ret_1d == pytest.approx(1.5)        # 将来リターンは残る
    assert row.fwd_excess_1d == pytest.approx(0.7)


def test_将来リターンのNoneは既存値を潰さない(temp_db):
    """まだ確定していない期間を None で送っても、確定済みを消さない。"""
    db.upsert_sector_flow_features([_record()])
    db.update_sector_forward_returns({
        "2026-08-28|3650": {"fwd_ret_1d": 1.5, "fwd_ret_5d": 3.0},
    })
    db.update_sector_forward_returns({
        "2026-08-28|3650": {"fwd_ret_1d": 1.5, "fwd_ret_5d": None},
    })

    with Session(temp_db) as session:
        row = session.query(SectorFlowFeatureDaily).one()
    assert row.fwd_ret_5d == pytest.approx(3.0)


def test_存在しない行への将来リターン更新は無視する(temp_db):
    updated = db.update_sector_forward_returns({
        "2026-08-28|9999": {"fwd_ret_1d": 1.0},
    })
    assert updated == 0


def test_必須項目が欠けたレコードは捨てる(temp_db):
    broken = _record()
    del broken["s33_code"]

    saved = db.upsert_sector_flow_features([broken, _record()])
    assert saved == 1


def test_空リストは何もしない(temp_db):
    assert db.upsert_sector_flow_features([]) == 0
    assert db.update_sector_forward_returns({}) == 0


def test_期間と業種で絞って読み出せる(temp_db):
    db.upsert_sector_flow_features([
        _record(date="2026-08-26", s33="3650"),
        _record(date="2026-08-27", s33="3650"),
        _record(date="2026-08-27", s33="3200"),
    ])

    df = db.get_sector_flow_features_df(from_date="2026-08-27", s33_code="3650")
    assert len(df) == 1
    assert df.iloc[0]["date"] == "2026-08-27"

    assert db.get_saved_sector_feature_dates() == ["2026-08-27", "2026-08-26"]


def test_データが無ければ空のDataFrameを返す(temp_db):
    assert db.get_sector_flow_features_df().empty
    assert db.get_saved_sector_feature_dates() == []


def test_特徴量の保存は空売り比率テーブルに触れない(temp_db):
    db.upsert_short_ratio_records([{
        "Date": "2026-08-28", "S33": "3650", "SectorName": "電気機器",
        "ShortRatioPct": 44.1, "TotalVolumeVa": 1000.0, "TotalShortVa": 441.0,
    }])
    db.upsert_sector_flow_features([_record()])

    with Session(temp_db) as session:
        assert session.query(ShortRatioDaily).one().short_ratio_pct == 44.1
        assert len(session.query(SectorFlowFeatureDaily).all()) == 1
