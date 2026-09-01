"""
日次パイプラインへの騰落銘柄数の組み込みが fail-soft であることのテスト。

空売り比率の取得0件はパイプラインを落とす（無通知の欠測を防ぐため）が、
騰落銘柄数は文脈情報なので、失敗しても本処理を止めてはならない。
ここを取り違えると、J-Quants の一時障害で AI レポートまで欠落する。
"""
import pytest

from scripts import fetch_short_ratio
from src.data_fetcher.jquants_api_client import (
    JQuantsNotEntitledError,
    JQuantsRequestError,
)


class _FakeClient:
    """J-Quants クライアントの代役。"""

    def __init__(self, *, configured=True, raises=None):
        self.is_configured = configured
        self._raises = raises

    def get_trading_calendar(self, from_date, to_date):
        if self._raises:
            raise self._raises
        return [
            {"Date": "2026-08-27", "HolDiv": "1"},
            {"Date": "2026-08-28", "HolDiv": "1"},
        ]

    def get_daily_bars(self, target_date):
        return [
            {"Code": "1000", "C": 110, "AdjC": 110 if target_date == "2026-08-28" else 100},
        ]

    def get_listed_master(self, target_date=None):
        # S33 は業種別フロー特徴量の母集団に必須（欠けると業種が1つも作られない）
        return [{"Code": "1000", "Mkt": "0111", "S33": "3650"}]

    def get_topix_bars(self, from_date, to_date):
        return [
            {"Date": "2026-08-27", "C": 4117.22},
            {"Date": "2026-08-28", "C": 4146.71},
        ]


@pytest.fixture
def saved_records(monkeypatch):
    """DB書き込みを捕まえる。本番DBには触らない。

    業種別フロー特徴量の保存・読み出しもここで差し替える。差し替え漏れがあると
    テストが本番Supabaseへ接続してしまう（実際に一度ハングさせた）。
    """
    import pandas as pd

    captured = []

    def _fake_upsert(records):
        captured.append(records)
        return len(records)

    monkeypatch.setattr(fetch_short_ratio, "upsert_market_breadth_records", _fake_upsert)
    monkeypatch.setattr(
        fetch_short_ratio, "upsert_sector_flow_features", lambda records: len(records)
    )
    monkeypatch.setattr(
        fetch_short_ratio, "get_sector_flow_features_df",
        lambda **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        fetch_short_ratio, "update_sector_forward_returns", lambda values: 0
    )
    return captured


def _use_client(monkeypatch, client):
    monkeypatch.setattr(fetch_short_ratio, "JQuantsApiClient", lambda: client)


# ------------------------------------------------------------------
# 正常系
# ------------------------------------------------------------------
def test_取得できたら保存して件数を返す(monkeypatch, saved_records):
    _use_client(monkeypatch, _FakeClient())

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["error"] is None
    assert result["saved"] > 0
    assert saved_records, "保存が呼ばれていない"
    record = saved_records[0][0]
    assert record["date"] == "2026-08-28"
    assert record["topix_change_pct"] == pytest.approx(0.716, abs=0.01)


def test_前営業日は取引カレンダーで決める(monkeypatch, saved_records):
    """日付の引き算だと連休で1日ずれる。公式カレンダーに委ねる。"""
    requested = []

    class _RecordingClient(_FakeClient):
        def get_daily_bars(self, target_date):
            requested.append(target_date)
            return super().get_daily_bars(target_date)

    _use_client(monkeypatch, _RecordingClient())
    fetch_short_ratio._step_breadth("2026-08-28")

    assert requested == ["2026-08-28", "2026-08-27"]


# ------------------------------------------------------------------
# fail-soft（ここが本題）
# ------------------------------------------------------------------
def test_APIキー未設定でも例外を投げない(monkeypatch, saved_records):
    _use_client(monkeypatch, _FakeClient(configured=False))

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] == 0
    assert "APIキー未設定" in result["error"]
    assert not saved_records


def test_契約プラン外のエラーでも例外を投げない(monkeypatch, saved_records):
    _use_client(monkeypatch, _FakeClient(
        raises=JQuantsNotEntitledError("This API is not available on your subscription.")
    ))

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] == 0
    assert result["error"]
    assert not saved_records


def test_通信エラーでも例外を投げない(monkeypatch, saved_records):
    _use_client(monkeypatch, _FakeClient(raises=JQuantsRequestError("boom")))

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] == 0
    assert result["error"]


def test_想定外の例外でも本処理を止めない(monkeypatch, saved_records):
    """J-Quants 側の仕様変更で未知の例外が出てもレポート生成まで到達させる。"""
    _use_client(monkeypatch, _FakeClient(raises=ValueError("unexpected shape")))

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] == 0
    assert "unexpected shape" in result["error"]


def test_前営業日が特定できなくても例外を投げない(monkeypatch, saved_records):
    class _NoBusinessDayClient(_FakeClient):
        def get_trading_calendar(self, from_date, to_date):
            return [{"Date": "2026-08-28", "HolDiv": "1"}]  # 前営業日が無い

    _use_client(monkeypatch, _NoBusinessDayClient())

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] == 0
    assert "前営業日" in result["error"]
    assert not saved_records


# ------------------------------------------------------------------
# 業種別フロー特徴量（Phase 0）
# ------------------------------------------------------------------
def test_騰落銘柄数と同じ日足から業種特徴量も保存する(monkeypatch, saved_records):
    """API呼び出しを増やさず、取得済みの日足を使い回していることを確認する。"""
    calls = []

    class _CountingClient(_FakeClient):
        def get_daily_bars(self, target_date):
            calls.append(target_date)
            return super().get_daily_bars(target_date)

    _use_client(monkeypatch, _CountingClient())
    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["features_saved"] > 0
    # 日足の取得は当日と前日の2回だけ（特徴量のために追加取得していない）
    assert calls == ["2026-08-28", "2026-08-27"]


def test_業種特徴量の保存が失敗しても騰落銘柄数は保存済みのままにする(monkeypatch, saved_records):
    """記録の失敗で、先に成功した保存を巻き戻さない。"""
    _use_client(monkeypatch, _FakeClient())
    monkeypatch.setattr(
        fetch_short_ratio, "upsert_sector_flow_features",
        lambda records: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    result = fetch_short_ratio._step_breadth("2026-08-28")

    assert result["saved"] > 0          # 騰落銘柄数は保存できている
    assert result["features_saved"] == 0
    assert result["error"] is None      # ステップ全体は失敗扱いにしない
    assert saved_records


def test_将来リターンの更新は直近窓に限定する(monkeypatch, saved_records):
    """テーブル全体を毎日読み直すと、行数の増加に比例して遅くなる。"""
    import pandas as pd

    seen = {}

    def _fake_get(**kwargs):
        seen.update(kwargs)
        return pd.DataFrame()

    _use_client(monkeypatch, _FakeClient())
    monkeypatch.setattr(fetch_short_ratio, "get_sector_flow_features_df", _fake_get)
    fetch_short_ratio._step_breadth("2026-08-28")

    assert seen.get("to_date") == "2026-08-28"
    assert seen.get("from_date") < "2026-08-28"   # 全期間ではなく窓で読んでいる


# ------------------------------------------------------------------
# 空売り比率側の厳しさは変えていないこと
# ------------------------------------------------------------------
def test_空売り比率の取得0件は従来どおり例外にする(monkeypatch):
    """騰落銘柄数を fail-soft にした影響で、本体まで緩めていないことを確認する。"""
    monkeypatch.setattr(
        fetch_short_ratio, "fetch_and_store_short_ratio_date",
        lambda date: {"saved_sector": 0, "saved_market": 0, "target_date": date},
    )

    with pytest.raises(RuntimeError, match="業種別データを1件も取得できませんでした"):
        fetch_short_ratio._step_fetch("2026-08-28", days=5)
