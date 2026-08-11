"""業種別株価指数の解析と4象限判定のテスト（ネットワーク非依存）。"""
import pytest

from config.sectors import SECTORS_S33
from src.macro_context import sector_price
from src.macro_context.sector_price import (
    _parse_history,
    build_sector_returns,
    fetch_sector_returns,
    format_quadrant,
)

# 実ファイルと同じ形式。33業種ぶんの値を持つ2営業日 + 1年前の参照行
_V1 = ",".join(f"{100 + i}.00" for i in range(33))
_V2 = ",".join(f"{101 + i}.00" for i in range(33))   # 各業種が +1.00
_V_OLD = ",".join(f"{50 + i}.00" for i in range(33))

SAMPLE_JS = f'''var GY=[];q=0;
GY[q]="2026/08/10,15:30,{_V2}";q++;
GY[q]="2026/08/07,15:30,{_V1}";q++;
GY[q]="2025/08/08,15:30,{_V_OLD}";q++;
'''


def test_parse_history_reads_every_business_day():
    history = _parse_history(SAMPLE_JS)

    assert set(history) == {"2026-08-10", "2026-08-07", "2025-08-08"}
    assert len(history["2026-08-10"]) == 33


def test_parse_history_skips_rows_with_wrong_sector_count():
    """並び順で業種を同定しているため、本数が違う行は使わない。"""
    broken = 'var GY=[];q=0;\nGY[q]="2026/08/10,15:30,100.0,200.0";q++;\n'

    assert _parse_history(broken) == {}


def test_parse_history_skips_malformed_dates_and_values():
    bad = (
        'GY[q]="2026-08-10,15:30,' + _V1 + '";q++;\n'      # 日付形式が違う
        'GY[q]="2026/08/09,15:30,' + ",".join(["x"] * 33) + '";q++;\n'  # 数値でない
    )

    assert _parse_history(bad) == {}


# ------------------------------------------------------------------
# 騰落率の算出
# ------------------------------------------------------------------

def test_returns_map_to_tse_sector_codes_in_order():
    """並び順が東証33業種マスタと一致していることを担保する。"""
    rows = build_sector_returns(_parse_history(SAMPLE_JS), "2026-08-10")

    assert len(rows) == 33
    expected = [c for c in SECTORS_S33 if c != "9999"]
    assert [r["s33_code"] for r in rows] == expected
    assert rows[0]["sector_name"] == "水産・農林業"


def test_returns_are_computed_from_consecutive_business_days():
    rows = build_sector_returns(_parse_history(SAMPLE_JS), "2026-08-10")
    first = rows[0]

    assert first["prev_date"] == "2026-08-07"
    assert first["index_value"] == pytest.approx(101.0)
    assert first["prev_value"] == pytest.approx(100.0)
    assert first["change_pct"] == pytest.approx(1.0)


def test_year_old_reference_row_is_not_used_as_previous_day():
    """履歴末尾には1年前の参照行が混ざる。これを前日として扱わない。"""
    rows = build_sector_returns(_parse_history(SAMPLE_JS), "2026-08-07")

    assert rows == []


def test_returns_empty_for_date_outside_history():
    assert build_sector_returns(_parse_history(SAMPLE_JS), "2026-01-05") == []
    assert build_sector_returns({}, "2026-08-10") == []


def test_latest_date_is_used_when_target_is_omitted():
    rows = build_sector_returns(_parse_history(SAMPLE_JS))

    assert rows[0]["as_of"] == "2026-08-10"


# ------------------------------------------------------------------
# 取得の fail-soft
# ------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def test_fetch_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setattr(
        sector_price.requests, "get", lambda *a, **k: _FakeResponse(503)
    )

    assert fetch_sector_returns() == []


def test_fetch_returns_empty_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise sector_price.requests.ConnectionError("boom")

    monkeypatch.setattr(sector_price.requests, "get", boom)

    assert fetch_sector_returns() == []


def test_fetch_returns_empty_when_format_changes(monkeypatch):
    """配信形式が変わったら黙って空を返す（誤った数値を作らない）。"""
    monkeypatch.setattr(
        sector_price.requests, "get",
        lambda *a, **k: _FakeResponse(200, "<html>maintenance</html>"),
    )

    assert fetch_sector_returns() == []


def test_fetch_parses_real_format(monkeypatch):
    monkeypatch.setattr(
        sector_price.requests, "get",
        lambda *a, **k: _FakeResponse(200, SAMPLE_JS),
    )

    rows = fetch_sector_returns("2026-08-10")

    assert len(rows) == 33
    assert rows[0]["change_pct"] == pytest.approx(1.0)


# ------------------------------------------------------------------
# 4象限
# ------------------------------------------------------------------

def test_quadrants_cover_all_four_combinations():
    assert "売り吸収" in format_quadrant(2.5, 1.2)
    assert "方向性売り優勢" in format_quadrant(2.5, -1.2)
    assert "ショートカバー主導" in format_quadrant(-2.5, 1.2)
    assert "買い不在" in format_quadrant(-2.5, -1.2)


def test_quadrant_is_blank_when_either_side_is_missing():
    """株価が取れない業種で象限を断定しない。"""
    assert format_quadrant(None, 1.2) == ""
    assert format_quadrant(2.5, None) == ""
    assert format_quadrant(0.0, 1.2) == ""      # 前日比ゼロは方向を決めない


def test_quadrant_wording_avoids_assertions():
    """すべて可能性の表現で、断定しない。"""
    for dod, pct in [(2.5, 1.2), (2.5, -1.2), (-2.5, 1.2), (-2.5, -1.2)]:
        label = format_quadrant(dod, pct)
        assert "可能性" in label


# ------------------------------------------------------------------
# プロンプト側の fail-soft
# ------------------------------------------------------------------

def test_prompt_builder_swallows_fetch_errors(monkeypatch):
    """株価が取れなくてもレポート生成を止めない。"""
    from src.ai_engine import prompt_builder

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(sector_price, "returns_by_sector_code", boom)

    assert prompt_builder._safe_sector_returns("2026-08-10") == {}
