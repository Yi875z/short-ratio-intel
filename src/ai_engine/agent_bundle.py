"""
サブスクの AI エージェント（ChatGPT の dots 等）に渡す「材料」と「作業手順書」を作る。

材料は Gemini に渡していたものと同じ指示（system）とデータ（user）に、出力の JSON Schema を添えた1ファイル。
エージェントはこれだけを根拠にレポートを書き、システムが scripts/import_agent_report.py で検証して載せる。
"""
from __future__ import annotations

import json

from src.ai_engine.output_schema import ReadingReport
from src.ai_engine.prompt_builder import build_system_prompt, build_user_prompt

INPUT_END_MARKER = "<!-- SHORT_RATIO_INPUT_END -->"


def build_agent_bundle(report_date: str, today_summary: dict, weekly_df, anomalies: list) -> str:
    """材料ファイルの本文を返す。末尾に INPUT_END_MARKER（途中までしか読めていないことの検知用）。"""
    system_prompt = build_system_prompt()
    # Tavily の自動取得は使わない（RSS 見出しは入る）。外部APIの枠をエージェント経路で消費しないため。
    user_prompt = build_user_prompt(
        report_date, today_summary, weekly_df, anomalies, "", auto_fetch_news=False
    )
    schema = json.dumps(ReadingReport.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"""# 空売り比率レポート 材料 {report_date}

この1ファイルに、レポートを書くための指示・データ・出力形式がすべて入っています。
ここに書かれていないデータを外部から調べて足さないでください（Web検索もしない）。

## 1. 指示（分析の規則）

{system_prompt}

## 2. データ（{report_date} 時点・システムが計算済み）

{user_prompt}

## 3. 出力形式（JSON Schema）

出力は次の JSON Schema に従う JSON オブジェクト1つだけ。説明文・コードブロック記号（```）は付けない。

{schema}

{INPUT_END_MARKER}
"""


def build_agent_instructions(folder_name: str) -> str:
    """エージェント向けの作業手順書（Drive の受け渡し用フォルダ直下に置く）。"""
    return f"""# 空売り比率アナリスト（dots）作業手順

あなたは short-ratio-intel（JPX空売り比率インテリジェンス）の日次レポート担当です。
システムが毎営業日 19:10 頃に材料ファイルを置くので、それを読んでレポートの JSON を書きます。
あなたが書いた JSON はシステムが 20:30 に検証してから画面に載せます。検証に通らないものは載りません。

## 毎営業日 19:40 にやること
1. Google Drive の `{folder_name}/inputs/` で、今日の日付（YYYY-MM-DD）の `<日付>_input.md` を開く。
   無ければ何もしない（休場日、または材料の配置が遅れている）。Slack に「材料なし」とだけ送る。
2. ファイルの最後に `{INPUT_END_MARKER}` が無ければ途中までしか読めていないので、作業を止めて Slack で知らせる。
3. ファイルの「1. 指示」に従い、「2. データ」だけを根拠に分析する。Web検索や外部データで補わない。
4. 「3. 出力形式」の JSON Schema どおりの JSON を作る。
5. `{folder_name}/outputs/<日付>_report.json` に **書き込む**。このファイルはシステムが空の状態で用意してある。
   **新しいファイルを作らず、この既存ファイルの中身を JSON で置き換えること**（新しく作るとシステムから見えない）。
   中身は JSON だけ。コードブロック記号や説明文を付けない。
6. 保存したら Slack に「<日付> の空売り比率レポートを保存しました」とだけ送る。

## 守ること
- 数値はデータにあるものだけを使う。前日値などを「想定」で作らない。
- 空売り比率は日次の売買代金フローで、残高・ポジションではない。比率の低下だけで買い戻しと断定しない。
- 売買の推奨・目標価格・確率は書かない。
"""
