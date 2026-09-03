"""
JPX内訳の欠損とデータ破壊に対する回帰テスト。

2026-09-01 に実際に起きた事故の再発防止:
  1. JPX一覧ページの取得が一度失敗すると空辞書がキャッシュされ、
     そのインスタンスは以後すべての日付でPDFを見つけられなくなった。
  2. その結果すべての日が stock-marketdata へフォールバックした。
     スクレイパーは内訳を持たず 0 を返す。
  3. upsert が無条件に上書きしたため、取得済みの正しい内訳が 0 に潰された。
  4. 画面は内訳から比率を再計算していたため「空売り比率 0%」と表示された。
"""
from datetime import date

import pandas as pd
import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.analyzer.pressure_metrics import build_pressure_metrics
from src.data_fetcher.jpx_pdf_client import JPXShortSellingClient
from src.storage import db
from src.storage.models import Base, MarketShortRatioDaily, ShortRatioDaily


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    return engine


def _jpx_record(date="2026-08-28"):
    """JPX公式PDF由来（内訳あり）。"""
    return {
        "Date": date,
        "SellExShortVa": 5_140_754.0,
        "ShrtWithResVa": 3_201_367.0,
        "ShrtNoResVa": 751_240.0,
        "TotalShortVa": 3_952_607.0,
        "TotalVolumeVa": 9_093_361.0,
        "ShortRatioPct": 43.47,
        "DodChange": None,
    }


def _scraper_record(date="2026-08-28"):
    """stock-marketdata 由来（比率と売買代金のみ・内訳は0）。"""
    return {
        "Date": date,
        "SellExShortVa": 0,
        "ShrtWithResVa": 0,
        "ShrtNoResVa": 0,
        "TotalShortVa": 0,
        "TotalVolumeVa": 9_093_361.0,
        "ShortRatioPct": 43.50,
        "DodChange": 0.5,
    }


# ------------------------------------------------------------------
# 1. 一覧ページの失敗をキャッシュしない
# ------------------------------------------------------------------
def test_一覧ページの取得失敗をキャッシュしない(monkeypatch):
    """一度の通信失敗で、以後ずっとPDFを諦めてしまわないこと。"""
    calls = {"n": 0}

    def _fail(*args, **kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("transient")

    monkeypatch.setattr(requests, "get", _fail)
    client = JPXShortSellingClient()

    assert client._get_pdf_url_map() == {}
    assert client._get_pdf_url_map() == {}
    assert calls["n"] == 2, "失敗を覚え込んで再試行しなくなっている"


def test_成功した一覧ページはキャッシュする(monkeypatch):
    """毎回取りに行くと無駄なので、成功時はキャッシュする。"""
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        text = '<a href="/x/260828-m.pdf">m</a>'

        def raise_for_status(self):
            return None

    def _ok(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(requests, "get", _ok)
    client = JPXShortSellingClient()

    first = client._get_pdf_url_map()
    client._get_pdf_url_map()
    assert first
    assert calls["n"] == 1


# ------------------------------------------------------------------
# 2. 内訳を0で潰さない
# ------------------------------------------------------------------
def test_内訳なしの取得で既存の内訳を潰さない(temp_db):
    """これが今回の事故の本体。"""
    db.upsert_market_short_ratio_records([_jpx_record()])
    db.upsert_market_short_ratio_records([_scraper_record()])

    with Session(temp_db) as session:
        row = session.query(MarketShortRatioDaily).one()

    # 内訳は守られる
    assert row.total_short_va == pytest.approx(3_952_607.0)
    assert row.shrt_with_res_va == pytest.approx(3_201_367.0)
    assert row.sell_ex_short_va == pytest.approx(5_140_754.0)
    # 比率と売買代金は新しい取得結果で更新される
    assert row.short_ratio_pct == pytest.approx(43.50)
    assert row.dod_change == pytest.approx(0.5)


def test_内訳ありの取得は上書きする(temp_db):
    """正しい内訳同士なら、新しいほうで更新されること。"""
    db.upsert_market_short_ratio_records([_scraper_record()])
    db.upsert_market_short_ratio_records([_jpx_record()])

    with Session(temp_db) as session:
        row = session.query(MarketShortRatioDaily).one()
    assert row.total_short_va == pytest.approx(3_952_607.0)


def test_内訳なし同士なら0のまま保存する(temp_db):
    db.upsert_market_short_ratio_records([_scraper_record()])
    with Session(temp_db) as session:
        row = session.query(MarketShortRatioDaily).one()
    assert row.total_short_va == 0
    assert row.short_ratio_pct == pytest.approx(43.50)


# ------------------------------------------------------------------
# 3. 内訳なしを0%として表示・判定しない
# ------------------------------------------------------------------
def _history(rows):
    return pd.DataFrame(rows)


def _row(date, ratio, volume, short=None):
    """short=None なら内訳なし（スクレイパー由来）。"""
    if short is None:
        return {
            "date": date, "short_ratio_pct": ratio, "total_volume_va": volume,
            "total_short_va": 0, "shrt_with_res_va": 0, "shrt_no_res_va": 0,
            "sell_ex_short_va": 0,
        }
    return {
        "date": date, "short_ratio_pct": ratio, "total_volume_va": volume,
        "total_short_va": short, "shrt_with_res_va": short * 0.8,
        "shrt_no_res_va": short * 0.2, "sell_ex_short_va": volume - short,
    }


def test_内訳なしの日でも空売り比率は取得元の値を使う():
    """内訳から再計算すると 0% になる。取得元の比率を正とする。"""
    metrics = build_pressure_metrics(
        "2026-08-28", _history([_row("2026-08-28", 43.50, 9_093_361.0)])
    )

    assert metrics.ratios.total_short_pct == pytest.approx(43.50)
    assert metrics.ratios.with_restriction_pct is None
    assert metrics.ratios.without_restriction_pct is None
    assert metrics.values.total_short_va is None
    assert "JPX内訳（空売り代金）" in metrics.missing_inputs


def test_内訳ありの日は従来どおり内訳から比率を出す():
    metrics = build_pressure_metrics(
        "2026-08-28", _history([_row("2026-08-28", 43.47, 10_000_000.0, 4_000_000.0)])
    )
    assert metrics.ratios.total_short_pct == pytest.approx(40.0)
    assert metrics.ratios.with_restriction_pct == pytest.approx(32.0)
    assert metrics.values.total_short_va == pytest.approx(4_000_000.0)
    assert "JPX内訳（空売り代金）" not in metrics.missing_inputs


def test_内訳なしの日を空売り代金の平均やZスコアに混ぜない():
    """0 を平均に入れると「空売りが激減した日」に見えてしまう。"""
    rows = [_row(f"2026-08-{i:02d}", 40.0, 10_000_000.0, 4_000_000.0) for i in range(1, 8)]
    rows.append(_row("2026-08-08", 43.5, 10_000_000.0))   # 内訳なし
    rows.append(_row("2026-08-09", 40.0, 10_000_000.0, 4_100_000.0))

    metrics = build_pressure_metrics("2026-08-09", _history(rows))

    # 平均・Zスコアは欠測を詰めた窓で出す（サンプル数を併記しているため）
    assert metrics.short_value_change.vs_avg_pct == pytest.approx(2.5)


def test_前営業日が欠測なら前日比を出さない():
    """欠測を詰めて比べると、8/25 との比較を「前日比」と称してしまう。

    実際に 8/26〜8/31 が欠測だった 9/1 で、8/25 比が前日比として
    画面とAIプロンプトに出ていた。算出不能は None であって 0% ではない。
    """
    rows = [_row(f"2026-08-{i:02d}", 40.0, 10_000_000.0, 4_000_000.0) for i in range(1, 8)]
    rows.append(_row("2026-08-08", 43.5, 10_000_000.0))   # 内訳なし
    rows.append(_row("2026-08-09", 40.0, 10_000_000.0, 4_100_000.0))

    metrics = build_pressure_metrics("2026-08-09", _history(rows))

    assert metrics.short_value_change.dod_pct is None


def test_前営業日が揃っていれば前日比を出す():
    rows = [
        _row("2026-09-01", 40.0, 10_000_000.0, 4_000_000.0),
        _row("2026-09-02", 41.0, 10_000_000.0, 4_100_000.0),
    ]
    metrics = build_pressure_metrics("2026-09-02", _history(rows))

    assert metrics.short_value_change.dod_pct == pytest.approx(2.5)


def test_当日が欠測なら過去の値を当日の値として出さない():
    """末尾が欠測のとき、直前の内訳あり日が「当日の空売り代金」に化けていた。"""
    rows = [
        _row("2026-09-01", 40.0, 10_000_000.0, 4_000_000.0),
        _row("2026-09-02", 41.0, 10_000_000.0),               # 内訳なし
    ]
    metrics = build_pressure_metrics("2026-09-02", _history(rows))

    assert metrics.short_value_change.latest is None
    assert metrics.short_value_change.dod_pct is None
    assert metrics.values.total_short_va is None


def test_空売り比率の時系列は内訳なしの日も繋がる():
    rows = [
        _row("2026-08-26", 42.70, 7_378_717.0),          # 内訳なし
        _row("2026-08-27", 45.00, 8_711_234.0),          # 内訳なし
        _row("2026-09-01", 41.85, 8_436_078.0, 3_530_576.0),
    ]
    metrics = build_pressure_metrics("2026-09-01", _history(rows))

    assert metrics.total_ratio_change.latest == pytest.approx(41.85)
    # 45.00 → 41.85 の変化として繋がる（0%を挟まない）
    assert metrics.total_ratio_change.dod_pct == pytest.approx(-7.0, abs=0.1)


# ------------------------------------------------------------------
# 5. 月別アーカイブから過去日を取り直せる
#
# 一覧ページは直近2営業日ぶんしか載せない。それだけを見て「復旧不能」と
# 判断すると、実際には取れるデータを永久に諦めることになる（2026-09-03 に
# 実際にその誤判断をした）。欠測は必ずアーカイブまで見に行くこと。
# ------------------------------------------------------------------
_INDEX_HTML = """
<a href="/markets/statistics-equities/short-selling/t13aaa-att/260902-m.pdf">9/2</a>
<a href="/markets/statistics-equities/short-selling/t13aaa-att/260902-g.pdf">9/2</a>
"""

_ARCHIVE_AUG_HTML = """
<a href="/markets/statistics-equities/short-selling/t13bbb-att/260828-m.pdf">8/28</a>
<a href="/markets/statistics-equities/short-selling/t13bbb-att/260828-g.pdf">8/28</a>
<a href="/markets/statistics-equities/short-selling/t13ccc-att/260831-m.pdf">8/31</a>
"""


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        return None


def _routed_get(monkeypatch, archive_html=_ARCHIVE_AUG_HTML, on_archive=None):
    """一覧＝9/2のみ、アーカイブ01＝8月、他は404相当にした requests.get を仕込む。"""
    def _get(url, **kwargs):
        if url.endswith("/short-selling/"):
            return _Resp(_INDEX_HTML)
        if "00-archives-01.html" in url:
            if on_archive:
                on_archive(url)
            return _Resp(archive_html)
        raise requests.HTTPError(f"404 {url}")

    monkeypatch.setattr(requests, "get", _get)


def test_一覧に無い過去日はアーカイブから引く(monkeypatch):
    _routed_get(monkeypatch)
    client = JPXShortSellingClient()

    url = client._find_pdf_url("2026-08-28", "m")

    assert url and url.endswith("260828-m.pdf")


def test_一覧にある日はアーカイブを読みに行かない(monkeypatch):
    """毎日の取得でアーカイブまで叩くのは無駄。当日は一覧で完結すること。"""
    touched = []
    _routed_get(monkeypatch, on_archive=touched.append)
    client = JPXShortSellingClient()

    assert client._find_pdf_url("2026-09-02", "m").endswith("260902-m.pdf")
    assert touched == []


def test_同じ月を二度読みに行かない(monkeypatch):
    touched = []
    _routed_get(monkeypatch, on_archive=touched.append)
    client = JPXShortSellingClient()

    client._find_pdf_url("2026-08-28", "m")
    client._find_pdf_url("2026-08-31", "m")

    assert len(touched) == 1, "同じアーカイブページを何度も取りに行っている"


def test_アーカイブの採番は今月からの差で決まる():
    """01が前月。当月は一覧側にあるのでアーカイブには無い。"""
    today = date(2026, 9, 3)

    assert JPXShortSellingClient._archive_page_number("2026-08", today) == 1
    assert JPXShortSellingClient._archive_page_number("2026-07", today) == 2
    assert JPXShortSellingClient._archive_page_number("2025-09", today) == 12
    assert JPXShortSellingClient._archive_page_number("2026-09", today) is None   # 当月
    assert JPXShortSellingClient._archive_page_number("2025-08", today) is None   # 13ヶ月前


# ------------------------------------------------------------------
# 6. 業種別テーブルも内訳0で潰さない
#
# 市場全体だけ守っても、業種別は DELETE→INSERT で毎日入れ替わるため
# 内訳が1営業日ぶんずつ静かに失われ続ける。
# ------------------------------------------------------------------
def _sector_record(date="2026-08-28", with_breakdown=True):
    if with_breakdown:
        return {
            "Date": date, "S33": "3650", "SectorName": "電気機器",
            "SellExShortVa": 500_000.0, "ShrtWithResVa": 300_000.0,
            "ShrtNoResVa": 100_000.0, "TotalShortVa": 400_000.0,
            "TotalVolumeVa": 900_000.0, "ShortRatioPct": 44.4,
        }
    return {
        "Date": date, "S33": "3650", "SectorName": "電気機器",
        "SellExShortVa": 0, "ShrtWithResVa": 0, "ShrtNoResVa": 0,
        "TotalShortVa": 0, "TotalVolumeVa": 900_000.0, "ShortRatioPct": 44.5,
    }


def test_業種別も内訳なしの結果で既存の内訳を潰さない(temp_db):
    db.upsert_short_ratio_records([_sector_record()])
    db.upsert_short_ratio_records([_sector_record(with_breakdown=False)])

    with Session(temp_db) as session:
        row = session.query(ShortRatioDaily).one()

    assert row.total_short_va == 400_000.0, "内訳0の結果で既存の内訳が潰れている"
    assert row.shrt_with_res_va == 300_000.0
    assert row.short_ratio_pct == 44.5, "比率は新しい取得結果で更新されるべき"


def test_削除は内訳を持つ日だけに限る():
    """DELETE→INSERT を内訳なしの日にやると、既存の内訳ごと消える。"""
    records = [
        _sector_record("2026-09-02", with_breakdown=True),
        _sector_record("2026-09-01", with_breakdown=False),
    ]

    assert db.dates_with_breakdown(records) == ["2026-09-02"]


# ------------------------------------------------------------------
# 7. 出所を列に記録する（推測をやめる）
#
# 内訳4列は nullable=False, default=0 なので「未取得」と「本当に0」を
# 値では区別できない。全レイヤが同じヒューリスティクスを持ち回るのをやめ、
# 書いた側が事実を記録する。
# ------------------------------------------------------------------
def test_JPX由来とスクレイパー由来を列で見分けられる(temp_db):
    db.upsert_market_short_ratio_records([_jpx_record("2026-09-02")])
    db.upsert_market_short_ratio_records([_scraper_record("2026-09-01")])

    with Session(temp_db) as session:
        rows = {
            r.date: r.breakdown_source
            for r in session.query(MarketShortRatioDaily).all()
        }

    assert rows["2026-09-02"] == "jpx_pdf"
    assert rows["2026-09-01"] == "scraper"


def test_内訳を守った日は出所をスクレイパーに書き換えない(temp_db):
    """内訳はJPX由来のまま残っているので、出所もJPX由来のままが正しい。"""
    db.upsert_market_short_ratio_records([_jpx_record("2026-08-28")])
    db.upsert_market_short_ratio_records([_scraper_record("2026-08-28")])

    with Session(temp_db) as session:
        row = session.query(MarketShortRatioDaily).one()

    assert row.total_short_va == 3_952_607.0
    assert row.breakdown_source == "jpx_pdf"


def test_あとからJPX内訳が入れば出所も更新される(temp_db):
    db.upsert_market_short_ratio_records([_scraper_record("2026-08-28")])
    db.upsert_market_short_ratio_records([_jpx_record("2026-08-28")])

    with Session(temp_db) as session:
        row = session.query(MarketShortRatioDaily).one()

    assert row.breakdown_source == "jpx_pdf"


def test_業種別も出所を記録する(temp_db):
    db.upsert_short_ratio_records([_sector_record(with_breakdown=False)])

    with Session(temp_db) as session:
        assert session.query(ShortRatioDaily).one().breakdown_source == "scraper"


def test_判定は値ではなく出所の列を優先する():
    """列がある行では、もうヒューリスティクスを使わない。"""
    row = {
        "date": "2026-09-02", "short_ratio_pct": 44.8,
        "total_volume_va": 9_151_252.0, "total_short_va": 4_101_043.0,
        "shrt_with_res_va": 3_197_029.0, "shrt_no_res_va": 904_014.0,
        "breakdown_source": "jpx_pdf",
    }
    metrics = build_pressure_metrics("2026-09-02", _history([row]))

    assert metrics.values.total_short_va == pytest.approx(4_101_043.0)
    assert "JPX内訳（空売り代金）" not in metrics.missing_inputs
