import json
from datetime import datetime

from src.ai_engine.report_quality import (
    build_quality_comparison,
    build_quality_feedback_prompt_block,
    build_quality_history_row,
    build_quality_review_markdown,
    evaluate_report_quality,
)


def _complete_markdown(extra: str = "") -> str:
    return f"""
# 空売り比率 完全解読レポート
## 現在の支配的マクロ背景
## 東証全体サマリー
## JPX空売り内訳分析
## 市場テーマ判定
## シグナル履歴分析
## 投資判断ガードレール
本レポートは売買推奨ではなく、JPX日次売買代金フローを使った需給分析です。
反証条件と確認条件を必ず確認します。
未確認データは追加で見るべきデータとして分けます。
## Retail Trap vs Pro Intent
## 戦略的示唆
## 総括
## 次の監視ポイント
{extra}
"""


def _complete_json(
    theme_name: str = "金利",
    theme_shift_analysis: str = "テーマ転換は条件付きで監視する。",
) -> str:
    return json.dumps({
        "dominant_market_themes": [{"theme_name": theme_name}],
        "investment_guardrails": ["a", "b", "c"],
        "confirmation_conditions": ["a", "b", "c"],
        "false_positive_risks": ["a", "b", "c"],
        "additional_data_to_check": ["a", "b", "c"],
        "theme_shift_analysis": theme_shift_analysis,
        "theme_sector_alignment": "業種別フローとの整合性を確認する。",
    }, ensure_ascii=False)


def test_report_quality_passes_complete_report():
    summary = evaluate_report_quality(_complete_markdown(), _complete_json())

    assert summary.status_label == "OK"
    assert summary.failed_items == []
    assert summary.score_pct == 100.0


def test_report_quality_detects_lint_issue():
    markdown = _complete_markdown() + "\n機関投資家による確信的な方向性売り。"

    summary = evaluate_report_quality(markdown, _complete_json())

    assert summary.status_label == "要修正"
    assert any(item.check_name == "overconfidence" for item in summary.failed_items)


def test_report_quality_warns_when_structured_json_missing():
    summary = evaluate_report_quality(_complete_markdown(), "")

    assert summary.status_label == "要確認"
    assert any(item.check_name == "JSON保存" for item in summary.failed_items)


def test_report_quality_passes_theme_transition_reflection():
    theme_transition_context = """
【市場テーマ履歴比較】
- テーマ変化:
  - [強化] 米金利: 今回5.0 / 前回3.0 / 差分+2.0
"""
    summary = evaluate_report_quality(
        _complete_markdown(),
        _complete_json(
            theme_name="米金利",
            theme_shift_analysis="米金利テーマが強化しており、条件付きで監視する。",
        ),
        theme_transition_context=theme_transition_context,
    )

    assert not [
        item
        for item in summary.failed_items
        if item.category == "テーマ転換反映"
    ]


def test_report_quality_detects_theme_transition_not_reflected():
    theme_transition_context = """
【市場テーマ履歴比較】
- テーマ変化:
  - [強化] 米金利: 今回5.0 / 前回3.0 / 差分+2.0
"""
    summary = evaluate_report_quality(
        _complete_markdown(),
        _complete_json(
            theme_name="原油",
            theme_shift_analysis="テーマ転換は条件付きで監視する。",
        ),
        theme_transition_context=theme_transition_context,
    )

    assert any(
        item.check_name == "主要テーマ名反映"
        for item in summary.failed_items
    )
    assert any(
        item.check_name == "テーマ転換分析反映"
        for item in summary.failed_items
    )


def test_quality_feedback_prompt_block_is_empty_when_no_failures():
    summary = evaluate_report_quality(_complete_markdown(), _complete_json())

    assert build_quality_feedback_prompt_block(summary) == ""


def test_quality_feedback_prompt_block_lists_failed_items():
    summary = evaluate_report_quality(
        _complete_markdown(),
        "",
        theme_transition_context="""
【市場テーマ履歴比較】
- テーマ変化:
  - [強化] 米金利: 今回5.0 / 前回3.0 / 差分+2.0
""",
    )

    block = build_quality_feedback_prompt_block(summary)

    assert "前回AIレポート品質チェックからの改善メモ" in block
    assert "JSON保存" in block
    assert "市場テーマ履歴・転換メモ" in block
    assert "high項目は必ず解消" in block


def test_quality_review_markdown_contains_summary_and_feedback():
    summary = evaluate_report_quality(_complete_markdown(), "")
    feedback = build_quality_feedback_prompt_block(summary)

    markdown = build_quality_review_markdown(
        summary,
        report_date="2026-05-18",
        quality_feedback=feedback,
    )

    assert "# AIレポート品質レビュー 2026-05-18" in markdown
    assert "- 判定: 要確認" in markdown
    assert "## 未通過項目" in markdown
    assert "## 再生成用改善メモ" in markdown
    assert "JSON保存" in markdown


def test_quality_history_row_summarizes_report_quality():
    row = build_quality_history_row(
        "2026-05-18",
        _complete_markdown(),
        _complete_json(),
        model_used="gemini-test",
        generated_at=datetime(2026, 5, 18, 17, 30),
    )

    assert row["date"] == "2026-05-18"
    assert row["status"] == "OK"
    assert row["score_pct"] == 100.0
    assert row["failed_count"] == 0
    assert row["passed_count"] == row["total_checks"]
    assert row["model_used"] == "gemini-test"
    assert row["generated_at"] == "2026-05-18 17:30"


def test_quality_history_row_keeps_failed_counts():
    row = build_quality_history_row(
        "2026-05-18",
        _complete_markdown(),
        "",
    )

    assert row["status"] == "要確認"
    assert row["failed_count"] >= 1
    assert row["medium_count"] >= 1


def test_quality_comparison_detects_improvement():
    before = {
        "date": "2026-05-18",
        "status": "要修正",
        "score_pct": 65.0,
        "failed_count": 7,
        "high_count": 3,
    }
    after = {
        "date": "2026-05-18",
        "status": "要確認",
        "score_pct": 85.0,
        "failed_count": 2,
        "high_count": 0,
    }

    comparison = build_quality_comparison(before, after)

    assert comparison["result"] == "改善"
    assert comparison["score_delta"] == 20.0
    assert comparison["failed_delta"] == -5
    assert comparison["high_delta"] == -3


def test_quality_comparison_handles_new_report():
    after = {
        "date": "2026-05-19",
        "status": "OK",
        "score_pct": 100.0,
        "failed_count": 0,
        "high_count": 0,
    }

    comparison = build_quality_comparison(None, after)

    assert comparison["result"] == "新規生成"
    assert comparison["before_score_pct"] is None
    assert comparison["score_delta"] is None
