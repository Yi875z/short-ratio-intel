"""
画面に出す表・チャートの見出しを日本語に保つための決定論テスト。

DB やデータクラスの列名は英語のままなので、表示直前に _ja_frame で置き換えている。
データクラスに項目が増えたときに英語の見出しが画面へ漏れることを防ぐ。
"""
from dataclasses import fields

import pandas as pd

from app.streamlit_app import _COLUMN_LABELS, _SECTOR_TABLE_ORDER, _ja_frame
from src.analyzer.anomaly_detector import AnomalyEvent
from src.analyzer.flow_signal_analyzer import FlowSignal


def test_renames_known_columns():
    df = pd.DataFrame([{"sector_name": "電気機器", "short_ratio_pct": 45.8, "dod_change": -3.58}])

    out = _ja_frame(df)

    assert list(out.columns) == ["業種", "空売り比率(%)", "前日比(pt)"]


def test_leaves_unknown_columns_untouched():
    df = pd.DataFrame([{"sector_name": "鉄鋼", "未知の列": 1}])

    out = _ja_frame(df)

    assert list(out.columns) == ["業種", "未知の列"]


def test_translates_enum_values_not_just_headers():
    """dod_spike / medium といった区分値そのものも日本語にする。"""
    df = pd.DataFrame([{"event_type": "dod_spike", "severity": "medium"}])

    out = _ja_frame(df)

    assert out["種別"].iloc[0] == "前日比の急変"
    assert out["重要度"].iloc[0] == "中程度"


def test_unknown_enum_value_passes_through():
    df = pd.DataFrame([{"severity": "critical"}])

    assert _ja_frame(df)["重要度"].iloc[0] == "critical"


def test_empty_frame_is_returned_as_is():
    empty = pd.DataFrame()

    assert _ja_frame(empty).empty
    assert _ja_frame(None) is None


def test_does_not_mutate_the_input_frame():
    df = pd.DataFrame([{"event_type": "dod_spike"}])

    _ja_frame(df)

    assert df["event_type"].iloc[0] == "dod_spike"


def test_anomaly_event_fields_all_have_labels():
    """異常値テーブルに英語の見出しが漏れないこと（項目追加時に落ちる）。"""
    missing = [f.name for f in fields(AnomalyEvent) if f.name not in _COLUMN_LABELS]

    assert missing == []


def test_flow_signal_fields_all_have_labels():
    """機械判定シグナルの表も同様。"""
    missing = [f.name for f in fields(FlowSignal) if f.name not in _COLUMN_LABELS]

    assert missing == []


def test_sector_table_order_is_fully_labelled():
    missing = [column for column in _SECTOR_TABLE_ORDER if column not in _COLUMN_LABELS]

    assert missing == []


def test_labels_are_japanese():
    """対訳が英語のまま放置されていないこと（ASCIIだけの訳語を弾く）。"""
    ascii_only = [
        column for column, label in _COLUMN_LABELS.items()
        if label.isascii() and label not in {"URL"}
    ]

    assert ascii_only == []
