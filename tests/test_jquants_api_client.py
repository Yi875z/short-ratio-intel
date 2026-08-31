"""
J-Quants API v2 クライアントの回帰テスト（ネットワーク非依存）。

このプロジェクトは取得元の仕様変更で3営業日ぶん欠測した実績があるため、
「どう壊れたら、どう振る舞うか」をテストで固定しておく。
"""
import pytest
import requests

from src.data_fetcher.jquants_api_client import (
    JQuantsApiClient,
    JQuantsNotConfiguredError,
    JQuantsNotEntitledError,
    JQuantsRequestError,
    _normalize_date,
)


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """requests.Session の代役。呼ばれた URL とパラメータを記録する。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(responses, **kwargs):
    session = _FakeSession(responses)
    client = JQuantsApiClient(
        api_key="test-key",
        session=session,
        min_interval_sec=0,   # テストを待たせない
        max_retries=kwargs.pop("max_retries", 3),
        **kwargs,
    )
    return client, session


# ----------------------------------------------------------------------
# 認証と設定
# ----------------------------------------------------------------------
def test_未設定のキーでは専用の例外を投げる():
    client = JQuantsApiClient(api_key="", min_interval_sec=0)
    assert client.is_configured is False
    with pytest.raises(JQuantsNotConfiguredError):
        client.get_daily_bars("2026-08-28")


def test_APIキーはx_api_keyヘッダで送る():
    client, session = _client([_FakeResponse(200, {"data": []})])
    client.get_daily_bars("2026-08-28")
    assert session.calls[0]["headers"]["x-api-key"] == "test-key"


# ----------------------------------------------------------------------
# エンドポイントとパラメータ
# ----------------------------------------------------------------------
def test_日足は対象日をYYYY_MM_DDに正規化して渡す():
    client, session = _client([_FakeResponse(200, {"data": []})])
    client.get_daily_bars("20260828")
    assert session.calls[0]["url"].endswith("/equities/bars/daily")
    assert session.calls[0]["params"] == {"date": "2026-08-28"}


def test_銘柄一覧は対象日を渡すと母集団が対象日時点になる():
    client, session = _client([_FakeResponse(200, {"data": []})])
    client.get_listed_master("2026-08-28")
    assert session.calls[0]["params"] == {"date": "2026-08-28"}


def test_銘柄一覧は対象日を省略できる():
    client, session = _client([_FakeResponse(200, {"data": []})])
    client.get_listed_master()
    assert session.calls[0]["params"] == {}


def test_TOPIXは期間指定で取る():
    client, session = _client([_FakeResponse(200, {"data": []})])
    client.get_topix_bars("2026-08-20", "2026-08-28")
    assert session.calls[0]["url"].endswith("/indices/bars/daily/topix")
    assert session.calls[0]["params"] == {"from": "2026-08-20", "to": "2026-08-28"}


def test_日付の表記ゆれを吸収する():
    assert _normalize_date("2026-08-28") == "2026-08-28"
    assert _normalize_date("20260828") == "2026-08-28"
    assert _normalize_date("2026/08/28") == "2026-08-28"
    with pytest.raises(ValueError):
        _normalize_date("2026-8-32")


# ----------------------------------------------------------------------
# ページング
# ----------------------------------------------------------------------
def test_ページングキーを追って全件つなげる():
    responses = [
        _FakeResponse(200, {"data": [{"Code": "1"}], "pagination_key": "k1"}),
        _FakeResponse(200, {"data": [{"Code": "2"}]}),
    ]
    client, session = _client(responses)
    records = client.get_daily_bars("2026-08-28")

    assert [r["Code"] for r in records] == ["1", "2"]
    assert session.calls[1]["params"]["pagination_key"] == "k1"


def test_同じページングキーが返り続けても無限ループしない():
    responses = [
        _FakeResponse(200, {"data": [{"Code": "1"}], "pagination_key": "same"}),
        _FakeResponse(200, {"data": [{"Code": "2"}], "pagination_key": "same"}),
    ]
    client, session = _client(responses)
    records = client.get_daily_bars("2026-08-28")

    assert len(records) == 2
    assert len(session.calls) == 2


# ----------------------------------------------------------------------
# エラー処理
# ----------------------------------------------------------------------
def test_403は契約プラン外として即座に打ち切る():
    """Light では /markets/short-ratio が 403。再試行しても回復しないので粘らない。"""
    body = '{"message": "This API is not available on your subscription."}'
    client, session = _client([_FakeResponse(403, text=body)])

    with pytest.raises(JQuantsNotEntitledError) as excinfo:
        client.get_daily_bars("2026-08-28")

    assert len(session.calls) == 1  # 再試行していない
    assert "subscription" in str(excinfo.value)


def test_429は再試行して成功すれば値を返す():
    responses = [
        _FakeResponse(429, text="rate limited"),
        _FakeResponse(200, {"data": [{"Code": "1"}]}),
    ]
    client, session = _client(responses)
    assert len(client.get_daily_bars("2026-08-28")) == 1
    assert len(session.calls) == 2


def test_通信失敗は上限まで再試行してから例外にする():
    responses = [requests.ConnectionError("boom")] * 3
    client, session = _client(responses, max_retries=3)

    with pytest.raises(JQuantsRequestError):
        client.get_daily_bars("2026-08-28")
    assert len(session.calls) == 3


def test_想定外のステータスは再試行せず例外にする():
    client, session = _client([_FakeResponse(400, text="bad request")])
    with pytest.raises(JQuantsRequestError):
        client.get_daily_bars("2026-08-28")
    assert len(session.calls) == 1
