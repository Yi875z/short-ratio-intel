"""
AIレポートへ需給レジームを渡す仕組みのテスト（ネットワーク・DB非依存）。

狙いは「画面とレポートで違う結論が出る」ことを防ぐ点にある。
機械判定が THIN_MARKET（商いが細って比率だけ高い）と言っている日に、
レポートが「空売り比率が高く売り圧力が強い」と書くと、同じシステムが
2つの結論を出すことになる。
"""
import pandas as pd
import pytest

from src.ai_engine import prompt_builder
from src.ai_engine.gemini_client import GeminiReportGenerator
from src.ai_engine.output_schema import ReadingReport


def _market_df(days=25, total_volume=10_000_000.0, total_short=4_000_000.0):
    """直近N営業日の市場全体データ。既定は横ばい。"""
    rows = []
    for i in range(days):
        rows.append({
            "date": f"2026-08-{i + 1:02d}",
            "sell_ex_short_va": total_volume - total_short,
            "shrt_with_res_va": total_short * 0.8,
            "shrt_no_res_va": total_short * 0.2,
            "total_short_va": total_short,
            "total_volume_va": total_volume,
            "short_ratio_pct": total_short / total_volume * 100,
            "dod_change": None,
        })
    return pd.DataFrame(rows)


def _patch(monkeypatch, market_df, breadth_df=None):
    monkeypatch.setattr(
        prompt_builder, "get_market_short_ratio_df", lambda **kwargs: market_df
    )
    monkeypatch.setattr(
        prompt_builder, "get_market_breadth_df",
        lambda **kwargs: breadth_df if breadth_df is not None else pd.DataFrame(),
    )


# ------------------------------------------------------------------
# ブロックの中身
# ------------------------------------------------------------------
def test_機械判定レジームと根拠をプロンプトに載せる(monkeypatch):
    _patch(monkeypatch, _market_df())
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert "需給レジーム（機械判定" in block
    assert "この判定と矛盾する記述をしないこと" in block
    assert "判定:" in block
    assert "確信度:" in block


def test_絶対額の変化を比率と別に載せる(monkeypatch):
    """比率の水準だけでは「分母が縮んだだけ」をAIが判定できない。"""
    _patch(monkeypatch, _market_df())
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert "空売り代金と市場売買代金の変化" in block
    assert "総空売り代金:" in block
    assert "市場売買代金:" in block
    assert "5日平均比" in block
    assert "Zスコア" in block


def test_兆円で表示する(monkeypatch):
    _patch(monkeypatch, _market_df(total_volume=9_093_361.0, total_short=3_952_607.0))
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")
    assert "兆円" in block


def test_騰落銘柄数は対象市場が違う旨を添える(monkeypatch):
    """空売り集計と割り算させないための注意書き。"""
    breadth = pd.DataFrame([{
        "market_scope": "TSE_PRIME", "scope_label": "プライム",
        "advancing_issues": 873, "declining_issues": 635, "unchanged_issues": 49,
        "topix_close": 4146.71, "topix_prev_close": 4117.22, "topix_change_pct": 0.716,
    }])
    _patch(monkeypatch, _market_df(), breadth)

    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert "873" in block and "635" in block
    assert "対象市場が異なる" in block
    assert "TOPIX当日騰落率" in block


def test_未取得の入力を明示する(monkeypatch):
    """欠損を黙って推測で埋めさせないため、何が無いかを書く。"""
    _patch(monkeypatch, _market_df())
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert "未取得" in block
    assert "騰落銘柄数" in block


# ------------------------------------------------------------------
# fail-soft
# ------------------------------------------------------------------
def test_データが無くても空文字を返さない(monkeypatch):
    _patch(monkeypatch, pd.DataFrame())
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert block.strip()
    assert "データなし" in block


def test_例外が出てもレポート生成を止めない(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(prompt_builder, "get_market_short_ratio_df", _boom)
    block = prompt_builder.build_pressure_regime_prompt_block("2026-08-25")

    assert "取得失敗" in block
    assert "判定なしとして扱う" in block


# ------------------------------------------------------------------
# システムプロンプトの禁則
# ------------------------------------------------------------------
def test_システムプロンプトが比率と絶対額の分離を要求する():
    prompt = prompt_builder.build_system_prompt()

    assert "比率と絶対額を必ず分けて述べる" in prompt
    assert "THIN_MARKET" in prompt
    assert "矛盾する記述をしない" in prompt


def test_システムプロンプトが別軸であることを明示する():
    """既存の regime（リスクオン/オフ）と需給レジームは違う軸。"""
    prompt = prompt_builder.build_system_prompt()
    assert "別軸" in prompt


# ------------------------------------------------------------------
# 出力スキーマ
# ------------------------------------------------------------------
def test_需給レジーム欄がスキーマにあり既定値を持つ():
    report = ReadingReport.model_construct()
    assert hasattr(report, "supply_demand_regime_analysis")

    schema = ReadingReport.model_json_schema()
    field = schema["properties"]["supply_demand_regime_analysis"]
    assert "THIN_MARKET" in field["description"]


def _minimal_payload(omit: str) -> str:
    """スキーマ必須項目だけを埋め、指定の欄だけ落とした JSON を作る。

    スキーマが今後増えてもテストが壊れないよう、必須項目は動的に引く。
    """
    import json

    schema = ReadingReport.model_json_schema()
    payload = {}
    for name in schema.get("required", []):
        if name == omit:
            continue
        spec = schema["properties"][name]
        payload[name] = [] if spec.get("type") == "array" else "テスト"
    return json.dumps(payload, ensure_ascii=False)


def test_モデルが欄を落としてもレポート生成が止まらない():
    """3.7-flash は出力欄を落とすことがある。既定値で埋めて継続させる。"""
    report = GeminiReportGenerator._parse_response(
        _minimal_payload(omit="supply_demand_regime_analysis")
    )
    assert report.supply_demand_regime_analysis == "需給レジームの専用分析は未生成です。"


def test_マークダウンに需給レジームの節が出る():
    report = GeminiReportGenerator._parse_response(_minimal_payload(omit="")).model_copy(
        update={"supply_demand_regime_analysis": "薄商いによる見かけの高比率。"}
    )
    # _render_markdown は self を使わないため、インスタンス生成（APIキー必須）を避けて呼ぶ
    markdown = GeminiReportGenerator._render_markdown(None, report, "2026-08-28")

    assert "## ⚖️ 需給レジーム" in markdown
    assert "薄商いによる見かけの高比率。" in markdown
    # 東証全体サマリーの直後、JPX内訳分析の前に置く
    assert markdown.index("## ⚖️ 需給レジーム") < markdown.index("## 🧭 JPX空売り内訳分析")
