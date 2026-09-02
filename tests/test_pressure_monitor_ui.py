"""
需給モニタータブの表示ロジックのテスト（Streamlit実行なし）。

描画そのものは検証できないが、表示の直前に値を作る純粋関数と、
既存タブを壊していないことは固定できる。
"""
import pandas as pd
import pytest

from app.streamlit_app import (
    _PRESSURE_HISTORY_DAYS,
    _REGIME_COLORS,
    _regime_ratio_dod_pt,
    _safe_ratio_pct,
    _z_text,
)
from src.analyzer.pressure_metrics import ChangeBlock, build_pressure_metrics
from src.analyzer.pressure_regime import REGIME_LABELS


def test_比率計算は分母ゼロでNoneを返す():
    assert _safe_ratio_pct(400, 1000) == 40.0
    assert _safe_ratio_pct(400, 0) is None
    assert _safe_ratio_pct(None, 1000) is None
    assert _safe_ratio_pct(400, None) is None


def test_比率の前日比はptで返す():
    """40%→37% は -3pt であって -7.5% ではない。実務の読み方に合わせる。"""
    history = pd.DataFrame([
        {"date": "2026-08-27", "total_short_va": 4_000_000.0,
         "total_volume_va": 10_000_000.0, "shrt_with_res_va": 3_000_000.0,
         "shrt_no_res_va": 1_000_000.0, "sell_ex_short_va": 6_000_000.0,
         "short_ratio_pct": 40.0},
        {"date": "2026-08-28", "total_short_va": 3_700_000.0,
         "total_volume_va": 10_000_000.0, "shrt_with_res_va": 2_800_000.0,
         "shrt_no_res_va": 900_000.0, "sell_ex_short_va": 6_300_000.0,
         "short_ratio_pct": 37.0},
    ])
    metrics = build_pressure_metrics("2026-08-28", history)

    assert _regime_ratio_dod_pt(metrics) == pytest.approx(-3.0, abs=0.01)


def test_比率の前日比が無ければNone():
    history = pd.DataFrame([
        {"date": "2026-08-28", "total_short_va": 4_000_000.0,
         "total_volume_va": 10_000_000.0, "shrt_with_res_va": 3_000_000.0,
         "shrt_no_res_va": 1_000_000.0, "sell_ex_short_va": 6_000_000.0},
    ])
    metrics = build_pressure_metrics("2026-08-28", history)
    assert _regime_ratio_dod_pt(metrics) is None


def test_Zスコアの表示はサンプル不足を明示する():
    assert "サンプル不足" in _z_text(ChangeBlock(label="x"))
    assert _z_text(ChangeBlock(label="x", zscore=1.23, sample_size=20)) == "+1.23（n=20）"


def test_全レジームに表示色が定義されている():
    """未定義の色でグレーに落ちると、判定の重さが画面で伝わらない。"""
    for regime in REGIME_LABELS:
        assert regime in _REGIME_COLORS


def test_推移チャートは20営業日を見る():
    assert _PRESSURE_HISTORY_DAYS == 20


def test_既存タブの描画関数が残っている():
    """需給モニターの追加で既存タブを壊していないことを固定する。"""
    from app import streamlit_app

    for name in (
        "_render_overview",
        "_render_sectors",
        "_render_breakdown",
        "_render_market_theme_tab",
        "_render_market_data_tab",
        "_render_calendar_tab",
        "_render_ai_report_tab",
        "_render_history_tab",
        "_render_us_flow_tab",
    ):
        assert callable(getattr(streamlit_app, name)), name
