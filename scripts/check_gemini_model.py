"""
Gemini モデルの採用可否を「本番同等の入力」で判定するチェックスクリプト。

2026-08-24 の障害（AIレポート欠落）の再発防止用。
gemini-3.7-flash は短いプロンプトでは 3秒で返るのに、本番の入力
（system 67.8K字 / JSON出力 / max_output_tokens=32768）では 600秒を超えて 504 になり、
SDK の内部リトライが日次クォータを食い潰した。
つまり **短いプロンプトの疎通確認では採用可否を判断できない**。

このスクリプトは DB の実データから本番と同じ user prompt を組み立てて1回だけ呼び出し、
所要時間・生JSON長・スキーマ検証（_parse_response）の可否をまとめて出す。

使い方:
    python -m scripts.check_gemini_model                      # 既定モデルを検証
    python -m scripts.check_gemini_model gemini-3.7-flash     # 候補モデルを検証
    python -m scripts.check_gemini_model a b --date 2026-08-24

判定基準:
    - スキーマ検証を通ること（200 が返っただけでは不可）
    - 所要時間が GEMINI_REQUEST_TIMEOUT_SEC に対して十分な余裕を持つこと
      （目安: タイムアウトの半分以下。超える日が出るなら採用しない）

実測の基準値（2026-08-25 / 対象日 2026-08-24 / system 67,817字 + user 16,489字）:
    gemini-3.6-flash   61.7秒  生JSON 13,319字  スキーマ検証 OK  ← 現行の基準
    gemini-3.7-flash   ---     503 high demand  （8/14 504連発・8/24 600秒超と合わせ不採用）
注意:
    1モデルにつき無料枠を1リクエスト消費する（20 req/日・モデル単位）。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import google.generativeai as genai
from loguru import logger

from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_REQUEST_TIMEOUT_SEC,
)
from scripts.fetch_short_ratio import _prepare_analysis
from src.ai_engine.gemini_client import GeminiReportGenerator
from src.ai_engine.prompt_builder import build_system_prompt, build_user_prompt
from src.storage.db import get_latest_date


def _build_production_prompts(report_date: str) -> tuple[str, str]:
    """本番の AIレポート生成と同じ system / user プロンプトを組み立てる"""
    _, today_summary, weekly_df, anomalies, _ = _prepare_analysis(report_date)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        report_date,
        today_summary,
        weekly_df,
        anomalies,
        "",
        auto_fetch_news=False,   # Tavily の自動取得だけ切る（RSS は本番同様に入る）
    )
    return system_prompt, user_prompt


def check_model(model_name: str, system_prompt: str, user_prompt: str) -> dict:
    """1モデルを1回だけ呼び出し、結果を dict で返す（例外は握って結果に載せる）"""
    result: dict = {"model": model_name, "ok": False, "elapsed": 0.0, "detail": ""}
    model = genai.GenerativeModel(model_name=model_name, system_instruction=system_prompt)

    started = time.time()
    try:
        response = model.generate_content(
            user_prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.3,
                max_output_tokens=32768,
                response_mime_type="application/json",
            ),
            # 本番と同じ条件で測る（SDK 内部リトライを切って1呼び出し=1リクエスト）
            request_options={"retry": None, "timeout": GEMINI_REQUEST_TIMEOUT_SEC},
        )
        result["elapsed"] = time.time() - started
        raw = response.text
        result["chars"] = len(raw)

        # 「200 が返った」で終わらせない。本番と同じスキーマ検証まで通す。
        GeminiReportGenerator._parse_response(raw)
        result["ok"] = True
        result["detail"] = f"生JSON {len(raw):,}字 / スキーマ検証 OK"
    except Exception as e:
        result["elapsed"] = time.time() - started
        kind = GeminiReportGenerator._classify_error(str(e))
        result["detail"] = f"[{kind}] {str(e).splitlines()[0][:160]}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini モデルを本番同等の入力で検証する")
    parser.add_argument("models", nargs="*", default=[], help="検証するモデル名（省略時は既定モデル）")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は DB 最新日）")
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY が未設定です")
        return 1

    models = args.models or [GEMINI_MODEL]
    report_date = args.date or get_latest_date()
    if not report_date:
        logger.error("DB にデータがなく、対象日を決定できません")
        return 1

    genai.configure(api_key=GEMINI_API_KEY)
    system_prompt, user_prompt = _build_production_prompts(report_date)
    logger.info(
        f"検証対象日={report_date} / system={len(system_prompt):,}字 / "
        f"user={len(user_prompt):,}字 / timeout={GEMINI_REQUEST_TIMEOUT_SEC}秒"
    )

    results = [check_model(m, system_prompt, user_prompt) for m in models]

    print("\n=== 検証結果（本番同等の入力・1モデル1リクエスト）===")
    for r in results:
        mark = "OK  " if r["ok"] else "NG  "
        print(f"{mark} {r['model']:<24} {r['elapsed']:6.1f}秒  {r['detail']}")

    budget = GEMINI_REQUEST_TIMEOUT_SEC / 2
    for r in results:
        if r["ok"] and r["elapsed"] > budget:
            print(
                f"⚠️ {r['model']} は {r['elapsed']:.1f}秒。"
                f"タイムアウト {GEMINI_REQUEST_TIMEOUT_SEC}秒の半分（{budget:.0f}秒）を超えており採用は推奨しない。"
            )

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
