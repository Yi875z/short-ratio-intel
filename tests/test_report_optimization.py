"""
2026-09-30 のレポート最適化の回帰テスト。

- 出力スキーマ再編（31→19項目）と描画・固定ガードレール
- 表現lintの追加（残高語彙・誇張・想定値・テクニカル・機械判定との矛盾・古い主体別データ）
- ナレッジの章抽出と、見出しが消えたときの点検
- 投資主体別データの鮮度警告、AIレポート欠落の点検

lint のテストデータは、2026-09-30 の本番レポートに実際に出た文をそのまま使っている。
"""
from datetime import date

from src.ai_engine import prompt_builder as pb
from src.ai_engine.output_schema import ReadingReport, SectorAnalysis
from src.ai_engine.report_lint import lint_report_markdown
from src.ai_engine.report_renderer import STATIC_GUARDRAILS, render_report_markdown
from src.macro_context.institutional_flow import FLOW_STALE_AFTER_DAYS, flow_age_days
from src.macro_context.pipeline_health import (
    check_institutional_flow_freshness,
    check_knowledge_sections,
    check_report_gaps,
)


def _codes(markdown: str, input_text: str = "") -> set[str]:
    return {issue.code for issue in lint_report_markdown(markdown, input_text=input_text)}


# ──────────────────────────────────────────────────────────────
# 出力スキーマと描画
# ──────────────────────────────────────────────────────────────
def test_schema_has_nineteen_fields_and_no_boilerplate_fields():
    fields = set(ReadingReport.model_fields)
    assert len(fields) == 19
    # 毎日同じ文面の定型・重複・入力外データを誘発する欄は持たない
    for removed in (
        "investment_guardrails", "overall_conclusion", "strategic_suggestions",
        "market_overall_summary", "additional_data_to_check", "anomaly_commentary",
    ):
        assert removed not in fields


def test_minimal_json_still_parses_with_defaults():
    """モデルが欄を落としてもレポート生成は止めない（既定値はスキーマが持つ）。"""
    report = ReadingReport(executive_summary="結論")
    assert report.top_sectors_analysis == []
    assert "未生成" in report.supply_demand_regime_analysis


def test_old_report_json_is_accepted():
    """旧スキーマの保存JSONを流し込んでも落ちない（未知キーは無視）。"""
    ReadingReport(**{"executive_summary": "x", "strategic_suggestions": [{"title": "t"}]})


def test_renderer_always_prints_static_guardrails():
    markdown = render_report_markdown(ReadingReport(), "2026-09-30")
    for line in STATIC_GUARDRAILS:
        assert line in markdown
    assert "売買推奨ではなく" in markdown
    assert "日次売買代金フロー" in markdown


def test_renderer_shows_quadrant_and_interpretation():
    report = ReadingReport(top_sectors_analysis=[SectorAnalysis(
        sector_name="電気機器", short_ratio_pct=45.7, quadrant="売り吸収",
        interpretation="事実: 45.7%（+1.8pt）、株価+0.83%。",
    )])
    markdown = render_report_markdown(report, "2026-09-30")
    assert "電気機器（45.7%）" in markdown
    assert "売り吸収" in markdown


# ──────────────────────────────────────────────────────────────
# 表現lint（2026-09-30 の本番レポートに出た文）
# ──────────────────────────────────────────────────────────────
def test_flags_flow_described_as_balance():
    line = "実際には、価格規制ありの残高が高水準で残っており、株価が上値を伸ばせば踏み上げが誘発される。"
    assert "flow_as_balance" in _codes(line)


def test_header_note_negating_balance_is_not_flagged():
    line = "> 注: 本レポートの空売り比率はJPX日次売買代金フローであり、空売り残高・建玉ではありません。"
    assert "flow_as_balance" not in _codes(line)


def test_flags_hyperbole():
    assert "hyperbole" in _codes("市場構造はベアからブルへと完全に反転しており、")
    assert "hyperbole" in _codes("巨大な流動性津波によって売りポジションが洗い流された")


def test_flags_fabricated_number():
    assert "fabricated_number" in _codes("事実: 価格規制あり比率は34.7%で前日（36.5%想定）から低下した。")


def test_flags_technical_indicator_not_in_input():
    line = "- **リスク注意**: 急反落した場合、25日移動平均線割れで速やかに撤退する必要があります。"
    assert "technical_not_in_input" in _codes(line, input_text="業種別データ")


def test_technical_indicator_in_input_is_allowed():
    line = "25日移動平均線を上回った。"
    assert "technical_not_in_input" not in _codes(line, input_text="25日移動平均線: 38,000")


def test_flags_stale_flow_used_as_evidence():
    line = "解釈: Pro Intent（海外勢の先物買い）と主体別データは整合的で、裏付けています。"
    stale_input = "【機関フロー】\n- 【鮮度注意】このデータは分析日の19日前の週で…"
    assert "stale_flow_as_evidence" in _codes(line, stale_input)
    assert "stale_flow_as_evidence" not in _codes(line, "【機関フロー】新しい週")


def test_regime_contradiction_on_thin_market():
    input_text = "判定: THIN_MARKET（薄商い・見かけの高比率） / 確信度: medium"
    codes = _codes("空売り比率が高く売り圧力が強い一日となった。", input_text)
    assert "regime_contradiction" in codes


def test_regime_must_be_referenced():
    input_text = "判定: NEUTRAL（中立） / 確信度: low"
    assert "regime_not_referenced" in _codes("本文に判定名が無い。", input_text)
    assert "regime_not_referenced" not in _codes("機械判定は NEUTRAL（確信度low）。", input_text)


# ──────────────────────────────────────────────────────────────
# ナレッジの章抽出
# ──────────────────────────────────────────────────────────────
_FAKE_JPX = """# 02_JPX_Micro_Flows.md
## ChatGPT Projectでの使用ルール
ChatGPT向けの定型。入れない。
## 0. JPX空売り比率・価格規制内訳の解釈ルール
### 0.1 基本式
総空売り比率 = ...
## 1. 海外勢（CTA/HFT/Macro）
入れない。
## 2. 海外勢（Market Makers / Arbitrage）
裁定・ヘッジ。
### 2.1 小節
小節も入る。
## 3. 国内勢
入れない。
"""


def test_extract_sections_takes_only_matching_chapters_with_subsections():
    body, missing = pb._select_sections(
        _FAKE_JPX, ["JPX空売り比率・価格規制内訳", "海外勢（Market Makers / Arbitrage）"]
    )
    assert "基本式" in body and "小節も入る" in body
    assert "ChatGPT向け" not in body and "CTA/HFT" not in body and "国内勢" not in body
    assert missing == []


def test_missing_heading_is_reported_and_not_filled_with_whole_file():
    body = pb._extract_sections(_FAKE_JPX * 200, ["存在しない見出し"])
    assert len(body) <= pb._KNOWLEDGE_FALLBACK_CLIP + 50   # 丸ごとには戻さない


def test_missing_knowledge_sections_feeds_health_check():
    knowledge = {"jpx_micro": _FAKE_JPX, "user_rules": "", "project_protocol": ""}
    missing = pb.missing_knowledge_sections(knowledge)
    assert "現行の市場ルール" in missing["jpx_micro"]
    assert "user_rules" not in missing          # 未配置は別問題として鳴らさない
    issues = check_knowledge_sections(missing)
    assert issues and issues[0].severity == "medium"


# ──────────────────────────────────────────────────────────────
# 鮮度とレポート欠落
# ──────────────────────────────────────────────────────────────
def test_flow_age_days():
    assert flow_age_days("2026-09-11", "2026-09-30") == 19
    assert flow_age_days("", "2026-09-30") is None


def test_stale_institutional_flow_is_reported():
    today = date(2026, 9, 30)
    assert check_institutional_flow_freshness("2026-09-11", today)          # 19日前 → 鳴る
    assert not check_institutional_flow_freshness("2026-09-18", today)      # 12日前 → 正常
    assert not check_institutional_flow_freshness(None, today)              # 未接続は鳴らさない
    assert FLOW_STALE_AFTER_DAYS == 14


def test_stale_flow_warning_is_injected_into_prompt(monkeypatch):
    from src.macro_context import institutional_flow as flow

    snap = flow.InvestorFlowSnapshot(week_date="2026-09-11", flows=[
        flow.InvestorFlow(
            investor_type="foreigners", label="海外投資家",
            spot_net=-3037, futures_net_oku=5743, combined_net=2706, is_twin_engine=False,
        )
    ])
    monkeypatch.setattr(flow, "fetch_investor_flow", lambda target_date: snap)
    block = flow.build_institutional_flow_prompt_block("2026-09-30")
    assert "【鮮度注意】" in block and "19日前" in block


def test_report_gaps_are_detected():
    market = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    reports = ["2026-09-25", "2026-09-28", "2026-09-30"]
    issues = check_report_gaps(market, reports)
    assert issues and "2026-09-24" in issues[0].message and "2026-09-29" in issues[0].message
    assert issues[0].severity == "high"          # 後から生成できるので止めない
    assert not check_report_gaps(market, market)
