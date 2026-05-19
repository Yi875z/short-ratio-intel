import json

from src.ai_engine.report_quality import evaluate_report_quality


def _complete_markdown() -> str:
    return """
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
"""


def _complete_json() -> str:
    return json.dumps({
        "dominant_market_themes": [{"theme_name": "金利"}],
        "investment_guardrails": ["a", "b", "c"],
        "confirmation_conditions": ["a", "b", "c"],
        "false_positive_risks": ["a", "b", "c"],
        "additional_data_to_check": ["a", "b", "c"],
        "theme_shift_analysis": "テーマ転換は条件付きで監視する。",
        "theme_sector_alignment": "業種別フローとの整合性を確認する。",
    })


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
