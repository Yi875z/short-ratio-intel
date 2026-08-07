"""米国テーブルの保存ロジックテスト（一時SQLite・ネットワーク非依存）。

日本側のテーブル・関数には一切触れないことも併せて確認する。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.data_fetcher.finra_client import build_record
from src.data_fetcher.us_price_client import build_price_record
from src.storage import db
from src.storage.models import Base, ShortRatioDaily, UsMarketDaily, UsShortVolumeDaily


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """本番DBに触らない一時SQLiteへ差し替える。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    return engine


def _count(engine, model) -> int:
    with Session(engine) as session:
        return len(session.query(model).all())


# ------------------------------------------------------------------
# ショートボリューム
# ------------------------------------------------------------------

def test_upsert_accepts_records_with_missing_optional_fields(temp_db):
    """ソースによって取れないフィールドが None でも保存できる。

    ソース差異を None で吸収する契約により、下流で KeyError が構造的に起きない。
    """
    record = build_record(date_iso="2026-08-05", ticker="NVDA")
    assert record["ShortExemptVolume"] is None

    saved = db.upsert_us_short_volume_records([record])

    assert saved == 1
    with Session(temp_db) as session:
        row = session.query(UsShortVolumeDaily).one()
    assert row.short_exempt_volume is None
    assert row.short_ratio_pct is None
    assert row.ticker == "NVDA"


def test_upsert_is_idempotent_for_same_key(temp_db):
    """同一 (date, ticker, source) を二重投入しても行数は増えない。"""
    record = build_record(
        date_iso="2026-08-05", ticker="NVDA",
        short_volume=100.0, reported_total_volume=400.0,
    )

    db.upsert_us_short_volume_records([record])
    db.upsert_us_short_volume_records([record])

    assert _count(temp_db, UsShortVolumeDaily) == 1


def test_reupsert_updates_values_in_place(temp_db):
    """FINRAが数値を訂正した場合は上書きされる。"""
    db.upsert_us_short_volume_records([
        build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0)
    ])
    db.upsert_us_short_volume_records([
        build_record("2026-08-05", "NVDA", short_volume=200.0, reported_total_volume=400.0)
    ])

    with Session(temp_db) as session:
        row = session.query(UsShortVolumeDaily).one()
    assert row.short_volume == pytest.approx(200.0)
    assert row.short_ratio_pct == pytest.approx(50.0)


def test_duplicate_keys_within_one_batch_collapse_to_one_row(temp_db):
    """同じ日・銘柄が1回の呼び出しに二重で含まれても行は増えず、後の値が残る。"""
    first = build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0)
    second = build_record("2026-08-05", "NVDA", short_volume=300.0, reported_total_volume=400.0)

    db.upsert_us_short_volume_records([first, second])

    with Session(temp_db) as session:
        row = session.query(UsShortVolumeDaily).one()
    assert row.short_volume == pytest.approx(300.0)


def test_different_sources_are_stored_separately(temp_db):
    """同じ日・同じ銘柄でも報告元が違えば別レコードとして残す。"""
    finra = build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0)
    other = dict(finra, Source="NASDAQ_TRADER", VenueScope="EXCHANGE")

    db.upsert_us_short_volume_records([finra, other])

    assert _count(temp_db, UsShortVolumeDaily) == 2


def test_records_missing_required_keys_are_skipped_without_raising(temp_db):
    """必須項目が欠けた行は捨てる。例外でパイプライン全体を止めない。"""
    good = build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0)
    bad = dict(good, Ticker=None)

    saved = db.upsert_us_short_volume_records([good, bad])

    assert saved == 1
    assert _count(temp_db, UsShortVolumeDaily) == 1


def test_fractional_volumes_survive_the_round_trip(temp_db):
    """FINRA の小数を丸めずに保存する。"""
    db.upsert_us_short_volume_records([
        build_record(
            "2026-08-05", "NVDA",
            short_volume=22736741.981631,
            reported_total_volume=65072769.042101,
        )
    ])

    df = db.get_us_short_volume_df(date="2026-08-05")

    assert df.loc[0, "short_volume"] == pytest.approx(22736741.981631)


def test_get_dataframe_filters_and_returns_empty_when_absent(temp_db):
    db.upsert_us_short_volume_records([
        build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0),
        build_record("2026-08-05", "SMH", short_volume=50.0, reported_total_volume=200.0),
    ])

    assert len(db.get_us_short_volume_df(date="2026-08-05")) == 2
    assert len(db.get_us_short_volume_df(ticker="SMH")) == 1
    assert db.get_us_short_volume_df(date="2026-08-06").empty
    assert db.get_us_short_volume_latest_date() == "2026-08-05"


# ------------------------------------------------------------------
# 日足OHLCV
# ------------------------------------------------------------------

def test_market_daily_upsert_is_idempotent(temp_db):
    record = build_price_record(
        "2026-08-05", "NVDA", high=220.0, low=211.0, close=219.22, market_volume=157555300,
    )

    db.upsert_us_market_daily_records([record])
    db.upsert_us_market_daily_records([record])

    assert _count(temp_db, UsMarketDaily) == 1
    assert db.get_us_market_daily_df(ticker="NVDA").loc[0, "close"] == pytest.approx(219.22)


def test_market_daily_keeps_missing_values_as_null(temp_db):
    db.upsert_us_market_daily_records([build_price_record("2026-08-05", "NVDA")])

    with Session(temp_db) as session:
        row = session.query(UsMarketDaily).one()
    assert row.close is None
    assert row.market_volume is None


# ------------------------------------------------------------------
# 日本側への非干渉
# ------------------------------------------------------------------

def test_us_writes_do_not_touch_japanese_tables(temp_db):
    """米国データの投入で日本の業種別テーブルが変化しないこと。"""
    db.upsert_short_ratio_records([{
        "Date": "2026-08-05",
        "S33": "3650",
        "SectorName": "電気機器",
        "ShortRatioPct": 41.2,
    }])
    before = _count(temp_db, ShortRatioDaily)

    db.upsert_us_short_volume_records([
        build_record("2026-08-05", "NVDA", short_volume=100.0, reported_total_volume=400.0)
    ])
    db.upsert_us_market_daily_records([build_price_record("2026-08-05", "NVDA", close=219.22)])

    assert _count(temp_db, ShortRatioDaily) == before == 1
    assert db.get_short_ratio_df(date="2026-08-05").loc[0, "short_ratio_pct"] == pytest.approx(41.2)
