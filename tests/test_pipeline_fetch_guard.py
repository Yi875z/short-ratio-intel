"""
取得0件のときにパイプラインが黙って成功しないことを固定するテスト。

2026-08 の欠測は「取得0件でも後段がDBの既存データで走り切り、ワークフローが
success で終わる」ために3営業日ぶん気づかれなかった。同じ隠れ方を再発させない。
"""
import pytest

import scripts.fetch_short_ratio as pipeline


def test_業種別が0件なら例外を投げる(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "fetch_and_store_recent_short_ratio",
        lambda days: {"saved_sector": 0, "saved_market": 0, "target_date": ""},
    )
    with pytest.raises(RuntimeError, match="業種別データを1件も取得できませんでした"):
        pipeline._step_fetch(None, 5)


def test_東証全体だけ取れても業種別0件なら例外(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "fetch_and_store_recent_short_ratio",
        lambda days: {"saved_sector": 0, "saved_market": 5, "target_date": ""},
    )
    with pytest.raises(RuntimeError):
        pipeline._step_fetch(None, 5)


def test_取得できていれば結果をそのまま返す(monkeypatch):
    expected = {"saved_sector": 170, "saved_market": 5, "target_date": "2026-08-21"}
    monkeypatch.setattr(
        pipeline, "fetch_and_store_recent_short_ratio", lambda days: expected
    )
    assert pipeline._step_fetch(None, 5) == expected


def test_日付指定でも0件なら例外(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "fetch_and_store_short_ratio_date",
        lambda target_date: {"saved_sector": 0, "saved_market": 0, "target_date": target_date},
    )
    with pytest.raises(RuntimeError):
        pipeline._step_fetch("2026-08-21", 5)
