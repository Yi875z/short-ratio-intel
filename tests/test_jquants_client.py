"""
stock-marketdata.com スクレイパーのパース回帰テスト。

2026-08 に取得元が業種名（中点あり）・日付（和暦風表記）・列見出しを同時に変更し、
完全一致で引いていたパーサが全業種を取りこぼして3営業日ぶん欠測した。
新旧どちらの表記でも読めることをネットワーク非依存で固定する。
"""
import pytest

from src.data_fetcher.jquants_client import (
    JQuantsClient,
    _lookup_s33,
    _parse_table_date,
)

# 2026-08 以降の表記（中点・読点あり、日付は「2026年8月21日」、4列目は「売買代金」）
NEW_FORMAT_HTML = """
<html><body>
<table class="data-table">
<thead><tr><th>日付</th><th>空売り比率</th><th>前日比</th><th>売買代金</th></tr></thead>
<tbody>
<tr><td>2026年8月21日</td><td>42.7</td><td>+3.6</td><td>8,573,411</td></tr>
<tr><td>2026年8月20日</td><td>39.1</td><td>+0.1</td><td>9,281,612</td></tr>
</tbody>
</table>
<table class="data-table">
<thead><tr>
<th>日付</th><th>水産・農林業</th><th>ガラス・土石製品</th>
<th>証券、商品先物取引業</th><th>石油・石炭製品</th>
</tr></thead>
<tbody>
<tr><td>2026年8月21日</td><td>35.4</td><td>41.4</td><td>44.2</td><td>30.1</td></tr>
<tr><td>2026年8月20日</td><td>37.5</td><td>39.1</td><td>45.5</td><td>31.2</td></tr>
</tbody>
</table>
</body></html>
"""

# 2026-08 以前の表記（中点なし省略形、日付は「2026/08/14」、4列目は「売買代金合計」）
OLD_FORMAT_HTML = """
<html><body>
<table class="data-table">
<thead><tr><th>日付</th><th>空売り比率</th><th>前日比</th><th>売買代金合計</th></tr></thead>
<tbody>
<tr><td>2026/08/14</td><td>40.95</td><td>-0.79</td><td>11,167,022</td></tr>
</tbody>
</table>
<table class="data-table">
<thead><tr>
<th>日付</th><th>水産農林業</th><th>ガラス土石</th><th>証券商品先物</th><th>石油石炭製品</th>
</tr></thead>
<tbody>
<tr><td>2026/08/14</td><td>36.1</td><td>40.2</td><td>43.9</td><td>29.8</td></tr>
</tbody>
</table>
</body></html>
"""


@pytest.fixture
def client(monkeypatch):
    """HTML を差し替えられるクライアントを返すファクトリ。"""
    def _make(html):
        c = JQuantsClient()
        from bs4 import BeautifulSoup
        monkeypatch.setattr(
            c, "_fetch_soup", lambda: BeautifulSoup(html, "html.parser")
        )
        return c
    return _make


class TestSectorNameLookup:
    @pytest.mark.parametrize("name,expected", [
        ("水産・農林業", "0050"),
        ("水産農林業", "0050"),
        ("ガラス・土石製品", "3400"),
        ("ガラス土石", "3400"),
        ("証券、商品先物取引業", "7100"),
        ("証券商品先物", "7100"),
        ("石油・石炭製品", "3300"),
        ("電気・ガス業", "4050"),
        ("倉庫・運輸関連業", "5200"),
        ("情報・通信業", "5250"),
        ("パルプ・紙", "3150"),
        ("パルプ紙", "3150"),
        ("電気機器", "3650"),
    ])
    def test_新旧どちらの表記でもS33コードを引ける(self, name, expected):
        assert _lookup_s33(name) == expected

    def test_未知の表記はNoneを返す(self):
        assert _lookup_s33("空売り比率") is None
        assert _lookup_s33("") is None


class TestDateParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("2026年8月21日", "2026-08-21"),
        ("2026年12月1日", "2026-12-01"),
        ("2026/08/21", "2026-08-21"),
        ("2026-08-21", "2026-08-21"),
        ("  2026年8月21日  ", "2026-08-21"),
    ])
    def test_受け付ける日付表記(self, raw, expected):
        assert _parse_table_date(raw) == expected

    @pytest.mark.parametrize("raw", ["合計", "", "2026年8月", "21日"])
    def test_解釈できない日付はNone(self, raw):
        assert _parse_table_date(raw) is None


class TestSectorTable:
    def test_新表記の業種テーブルを読める(self, client):
        rows = client(NEW_FORMAT_HTML).get_short_ratio_by_date("2026-08-21")
        assert {r["S33"] for r in rows} == {"0050", "3400", "7100", "3300"}
        by_code = {r["S33"]: r for r in rows}
        assert by_code["0050"]["ShortRatioPct"] == 35.4
        assert by_code["0050"]["SectorName"] == "水産・農林業"
        assert all(r["Date"] == "2026-08-21" for r in rows)

    def test_旧表記の業種テーブルも読める(self, client):
        rows = client(OLD_FORMAT_HTML).get_short_ratio_by_date("2026-08-14")
        assert len(rows) == 4
        assert {r["S33"] for r in rows} == {"0050", "3400", "7100", "3300"}

    def test_直近N営業日は日付降順でまとまる(self, client):
        rows = client(NEW_FORMAT_HTML).get_recent_days(2)
        assert sorted({r["Date"] for r in rows}) == ["2026-08-20", "2026-08-21"]
        assert len(rows) == 8

    def test_該当日がなければ空リスト(self, client):
        assert client(NEW_FORMAT_HTML).get_short_ratio_by_date("2026-01-05") == []


class TestMarketTable:
    def test_新表記の東証全体テーブルを読める(self, client):
        row = client(NEW_FORMAT_HTML).get_market_short_ratio_by_date("2026-08-21")
        assert row is not None
        assert row["ShortRatioPct"] == 42.7
        assert row["DodChange"] == 3.6
        assert row["TotalVolumeVa"] == 8573411.0

    def test_旧見出し売買代金合計でも読める(self, client):
        row = client(OLD_FORMAT_HTML).get_market_short_ratio_by_date("2026-08-14")
        assert row is not None
        assert row["ShortRatioPct"] == 40.95
        assert row["DodChange"] == -0.79

    def test_該当日がなければNone(self, client):
        assert client(NEW_FORMAT_HTML).get_market_short_ratio_by_date("2026-01-05") is None


class TestFetchFailure:
    def test_取得失敗時は空を返す(self, monkeypatch):
        c = JQuantsClient()
        monkeypatch.setattr(c, "_fetch_soup", lambda: None)
        assert c.get_recent_days(5) == []
        assert c.get_market_recent_days(5) == []
