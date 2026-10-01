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
    # 確認条件の欄で「得られたら再評価する」と書くのは正しい扱い（dots の 9/24 で誤検知した実文）
    checklist = (
        "## ✅ 翌営業日の確認条件\n"
        "- 対象週・公表日を揃えた投資主体別の現物／先物フローが得られた時点で、テーマ候補と主体別の裏付けを再評価する"
    )
    assert "stale_flow_as_evidence" not in _codes(checklist, stale_input)


def test_flags_short_cover_assertion():
    """2026-10-01 の新形式レポートに残っていた断定。候補・可能性の形なら許す。"""
    assert "short_cover_asserted" in _codes("日銀短観を控え、空売りの新規手控えと買い戻しが強まった。")
    assert "short_cover_asserted" in _codes("新規売りが後退しショートカバーが入った。")
    assert "short_cover_asserted" not in _codes("新規売りが後退しショートカバーが入った可能性がある。")
    assert "short_cover_asserted" not in _codes("比率低下×株価上昇=ショートカバー候補（ポジション側データで要確認）")


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
    missing = pb.missing_knowledge_sections(knowledge, require_all=False)
    assert "現行の市場ルール" in missing["jpx_micro"]
    assert "user_rules" not in missing          # ローカル開発では未配置を鳴らさない
    issues = check_knowledge_sections(missing)
    assert issues and issues[0].severity == "medium"


def test_unregistered_required_knowledge_is_reported():
    """本番DBに upload されていないナレッジ（ナレッジ29など）を鳴らす（独立レビュー #2）。"""
    missing = pb.missing_knowledge_sections({"jpx_micro": _FAKE_JPX})
    assert missing["short_flow_pro"] == ["（未登録）"]
    assert missing["user_rules"] == ["（未登録）"]


def test_clipped_knowledge_is_reported():
    """抽出結果が上限を超えて末尾が落ちたら鳴らす（独立レビュー #12: 6,048字が6,000字で切れていた）。"""
    long_text = "## 0. JPX空売り比率・価格規制内訳の解釈ルール\n" + "あ" * (pb._KNOWLEDGE_SECTION_LIMIT + 10)
    missing = pb.missing_knowledge_sections(
        {"jpx_micro": long_text, "short_flow_pro": "x" * (pb._KNOWLEDGE_SECTION_LIMIT + 1)},
        require_all=False,
    )
    assert any("切り詰め" in p for p in missing["jpx_micro"])
    assert any("切り詰め" in p for p in missing["short_flow_pro"])


def test_empty_report_scores_lower_than_a_written_one():
    """中身が空のレポートが実レポートより高く採点される逆転を起こさない（独立レビュー #5）。"""
    from src.ai_engine.report_quality import evaluate_report_quality

    empty = ReadingReport()
    empty_q = evaluate_report_quality(render_report_markdown(empty, "2026-09-30"), empty.model_dump_json())

    written = ReadingReport(
        executive_summary="結論", supply_demand_regime_analysis="事実: NEUTRAL。",
        jpx_short_selling_breakdown_analysis="事実: 規制あり34.7%。", theme_shift_analysis="浮上中。",
        top_sectors_analysis=[SectorAnalysis(sector_name=n, short_ratio_pct=45.0, interpretation="i")
                              for n in ("a", "b", "c")],
        confirmation_conditions=["a", "b", "c"], false_positive_risks=["a", "b"],
        dominant_market_themes=[],
    )
    written_q = evaluate_report_quality(render_report_markdown(written, "2026-09-30"), written.model_dump_json())

    assert empty_q.status_label == "要修正"          # 本日の結論が空 → high
    assert written_q.score_pct > empty_q.score_pct


def test_position_data_names_are_not_flagged_as_balance():
    """信用残・建玉残高は見に行くべきポジション側のデータ名であり、誤検知しない（独立レビュー #5）。"""
    assert "flow_as_balance" not in _codes("- 信用取引残高（買い残の整理状況および売り残の増減）")
    assert "flow_as_balance" not in _codes("- 日経225オプションのStrike別詳細建玉残高")
    # 別データとして確認を求める文脈（ChatGPT 生成の検証で誤検知した実文）
    assert "flow_as_balance" not in _codes("週次投資主体別、国内金利、空売り残高が未確認で、断定できない。")
    assert "flow_as_balance" not in _codes("ショートカバー候補だが、残高データによる確認が必要。")
    # 本来の検出対象は引き続き捕まえる
    assert "flow_as_balance" in _codes("価格規制ありの残高が高水準で残っており、踏み上げが誘発される。")


def test_market_dod_is_filled_from_the_series_without_bridging_gaps():
    """東証全体の前日比が None のまま保存されていても、前営業日と比べて埋める（独立レビュー #4）。"""
    import pandas as pd

    from src.storage.db import fill_market_dod

    df = pd.DataFrame({
        "date": ["2026-09-17", "2026-09-18", "2026-09-24", "2026-09-28"],
        "short_ratio_pct": [41.39, 39.43, 41.05, 44.30],
        "dod_change": [None, None, None, None],
    })
    # 営業日の手がかり: 9/25 が営業日なのに欠けている → 9/28 は前営業日と比べられない
    trading = {"2026-09-17", "2026-09-18", "2026-09-24", "2026-09-25", "2026-09-28"}
    out = fill_market_dod(df, trading)

    assert out.loc[1, "dod_change"] == -1.96        # 9/17 → 9/18
    assert out.loc[2, "dod_change"] == 1.62         # 9/18 → 9/24（間は連休で営業日なし）
    assert out.loc[3, "dod_change"] is None         # 9/25 が欠けているので埋めない


def test_live_quotes_are_not_injected_for_past_dates(monkeypatch):
    """過去日のレポートに今日の市場気配を入れない（dots の指摘・2026-10-01）。"""
    monkeypatch.setattr(pb, "build_market_quotes_prompt_block", lambda: "LIVE_QUOTES")
    assert "対象日が過去" in pb._live_market_block_for("2000-01-04")
    assert pb._live_market_block_for("2999-12-31") == "LIVE_QUOTES"


def test_market_dod_keeps_stored_values():
    import pandas as pd

    from src.storage.db import fill_market_dod

    df = pd.DataFrame({"date": ["2026-09-17", "2026-09-18"], "short_ratio_pct": [41.0, 39.0],
                       "dod_change": [None, -9.9]})
    assert fill_market_dod(df, set()).loc[1, "dod_change"] == -9.9


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
