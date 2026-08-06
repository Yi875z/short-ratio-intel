"""FINRA CNMS クライアントの決定論テスト（ネットワーク非依存）。"""
import pytest
import requests

from src.data_fetcher import finra_client
from src.data_fetcher.finra_client import (
    FinraShortVolumeClient,
    build_record,
    compute_ratio,
    normalize_date,
    parse_cnms_text,
)

# 実データと同じ形式。ShortVolume / TotalVolume は小数を含む
SAMPLE_TEXT = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20260805|NVDA|22736741.981631|192126|65072769.042101|B,Q,N
20260805|BRK/B|333667.866980|446|1122781.253094|B,Q,N
20260805|AAA|8|0|4172.063696|Q
20260805|SMH|1079324.366614|3225|2502504.751430|B,Q,N
"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _client(tmp_path) -> FinraShortVolumeClient:
    """待ち時間ゼロ・一時ディレクトリをキャッシュに使うクライアント。"""
    return FinraShortVolumeClient(cache_dir=tmp_path, request_interval=0)


# ------------------------------------------------------------------
# パーサ
# ------------------------------------------------------------------

def test_parse_keeps_fractional_volumes_as_float():
    """FINRA の出来高は小数を含む。int で丸めたり例外にしたりしない。"""
    records = parse_cnms_text(SAMPLE_TEXT, tickers=["NVDA"])

    assert len(records) == 1
    record = records[0]
    assert record["ShortVolume"] == pytest.approx(22736741.981631)
    assert record["ReportedTotalVolume"] == pytest.approx(65072769.042101)
    assert record["ShortExemptVolume"] == pytest.approx(192126)


def test_parse_handles_slash_class_shares():
    """BRK/B のようなスラッシュ入りシンボルを壊さずに読む。"""
    records = parse_cnms_text(SAMPLE_TEXT, tickers=["BRK/B"])

    assert len(records) == 1
    assert records[0]["Ticker"] == "BRK/B"
    assert records[0]["ShortRatioPct"] == pytest.approx(29.7181, abs=1e-3)


def test_parse_skips_header_blank_and_malformed_lines():
    text = SAMPLE_TEXT + "\n\n壊れた行\n20260805|BAD|1|2\n"
    records = parse_cnms_text(text)

    assert [r["Ticker"] for r in records] == ["NVDA", "BRK/B", "AAA", "SMH"]


def test_parse_ignores_trailing_record_count_line():
    """実ファイル末尾にはレコード件数だけの行がある（例: "12233"）。データ行ではない。"""
    records = parse_cnms_text(SAMPLE_TEXT + "12233\r\r\n")

    assert len(records) == 4
    assert all(r["Ticker"] != "12233" for r in records)


def test_parse_strips_carriage_returns_from_last_column():
    """実ファイルは CRLF に余分な CR が付く。Market コードに \\r を残さない。"""
    text = SAMPLE_TEXT.replace("\n", "\r\r\n")
    records = parse_cnms_text(text, tickers=["NVDA"])

    assert records[0]["MarketCodes"] == "B,Q,N"


def test_parse_normalizes_date_to_iso():
    records = parse_cnms_text(SAMPLE_TEXT, tickers=["SMH"])
    assert records[0]["Date"] == "2026-08-05"


def test_parse_returns_empty_for_empty_text():
    assert parse_cnms_text("") == []
    assert parse_cnms_text(None) == []


# ------------------------------------------------------------------
# レコード契約（欠落フィールドで KeyError を出さない）
# ------------------------------------------------------------------

def test_build_record_always_contains_every_key():
    """取得できない値は None を明示的に入れる。キー欠落を構造的に起こさない。"""
    record = build_record(date_iso="2026-08-05", ticker="NVDA")

    expected_keys = {
        "Date", "Ticker", "Region", "Source", "VenueScope",
        "ShortVolume", "ShortExemptVolume", "ReportedTotalVolume",
        "ShortRatioPct", "MarketCodes",
    }
    assert set(record) == expected_keys
    assert record["ShortVolume"] is None
    assert record["ShortExemptVolume"] is None
    assert record["ShortRatioPct"] is None
    assert record["Region"] == "US"
    assert record["VenueScope"] == "OFF_EXCHANGE"


# ------------------------------------------------------------------
# 比率計算
# ------------------------------------------------------------------

def test_compute_ratio_uses_same_source_denominator():
    assert compute_ratio(50.0, 200.0) == pytest.approx(25.0)


def test_compute_ratio_returns_none_for_zero_or_missing_denominator():
    assert compute_ratio(100.0, 0) is None
    assert compute_ratio(100.0, None) is None
    assert compute_ratio(None, 100.0) is None


def test_compute_ratio_rejects_out_of_range_values():
    """分子が分母を超えるなど範囲外は判定不能として None（推測で埋めない）。"""
    assert compute_ratio(300.0, 100.0) is None
    assert compute_ratio(-1.0, 100.0) is None


# ------------------------------------------------------------------
# 日付正規化
# ------------------------------------------------------------------

def test_normalize_date_accepts_compact_and_iso():
    assert normalize_date("20260805") == "2026-08-05"
    assert normalize_date("2026-08-05") == "2026-08-05"


def test_normalize_date_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_date("2026/08/05")


# ------------------------------------------------------------------
# 取得（HTTP はすべてモック）
# ------------------------------------------------------------------

def test_access_denied_returns_empty_without_raising(tmp_path, monkeypatch):
    """非営業日は 403 が返る。例外にせず空リストを返し、キャッシュも作らない。"""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(403, "<Error><Code>AccessDenied</Code></Error>")

    monkeypatch.setattr(finra_client.requests, "get", fake_get)

    records = _client(tmp_path).get_daily_records("2026-08-02")

    assert records == []
    assert len(calls) == 1                      # 403 はリトライしない
    assert list(tmp_path.glob("*.txt")) == []   # 失敗を焼き付けない


def test_successful_fetch_is_cached_and_reused(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(200, SAMPLE_TEXT)

    monkeypatch.setattr(finra_client.requests, "get", fake_get)
    client = _client(tmp_path)

    first = client.get_daily_records("2026-08-05", tickers=["NVDA"])
    second = client.get_daily_records("2026-08-05", tickers=["NVDA"])

    assert len(first) == 1
    assert first == second
    assert len(calls) == 1                                       # 2回目はキャッシュ
    assert (tmp_path / "CNMSshvol20260805.txt").exists()


def test_network_error_retries_then_gives_up(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(finra_client.requests, "get", fake_get)
    monkeypatch.setattr(finra_client.time, "sleep", lambda _: None)

    assert _client(tmp_path).fetch_raw("2026-08-05") is None
    assert len(calls) == 3      # ネットワークエラーのみ3回まで


def test_unexpected_body_is_rejected(tmp_path, monkeypatch):
    """HTMLエラーページ等を掴んだら取り込まない。"""
    monkeypatch.setattr(
        finra_client.requests, "get",
        lambda url, **kwargs: _FakeResponse(200, "<html>maintenance</html>"),
    )

    assert _client(tmp_path).fetch_raw("2026-08-05") is None


def test_range_skips_weekends(tmp_path, monkeypatch):
    """土日はファイルが存在しないのでリクエストしない。"""
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        return _FakeResponse(200, SAMPLE_TEXT)

    monkeypatch.setattr(finra_client.requests, "get", fake_get)

    # 2026-08-01(土) 〜 2026-08-04(火)
    _client(tmp_path).get_range_records("2026-08-01", "2026-08-04", tickers=["NVDA"])

    assert len(requested) == 2   # 月・火のみ
    assert "20260803" in requested[0]
    assert "20260804" in requested[1]
