"""
Gemini API クライアント（モデルは config.settings.GEMINI_MODEL で指定）
レポート生成・JSON構造化出力
"""
import json
import time
from typing import Optional

import google.generativeai as genai
from loguru import logger

from config.settings import (
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GEMINI_MODEL_DEFAULT,
    GEMINI_MODEL_IS_OVERRIDDEN,
    GEMINI_REQUEST_TIMEOUT_SEC,
    GEMINI_ROUND_WAIT_SEC,
    GEMINI_TOTAL_BUDGET_SEC,
)
from src.ai_engine.output_schema import ReadingReport
from src.ai_engine.prompt_builder import build_system_prompt, build_user_prompt
from src.ai_engine.report_lint import lint_report_markdown
from src.ai_engine.report_renderer import render_report_markdown


class GeminiReportGenerator:
    """Gemini を使った空売り比率レポート生成クラス"""

    MAX_RETRIES = 3

    def __init__(self, max_rounds: int = 1):
        """
        Args:
            max_rounds: 全モデルが一時エラーで落ちたときに、待ってから巡回し直す回数の上限。
                既定の1は「一巡して駄目なら諦める」。画面の手動生成で数分待たせないため。
                定時パイプラインだけが settings.GEMINI_PIPELINE_MAX_ROUNDS を渡す。
        """
        self.max_rounds = max(1, max_rounds)
        if not GEMINI_API_KEY:
            raise ValueError(".env に GEMINI_API_KEY が設定されていません")

        genai.configure(api_key=GEMINI_API_KEY)
        self._system_instruction = build_system_prompt()
        self.model_name = GEMINI_MODEL
        self._model = self._build_model(self.model_name)

        # モデル指定の出所をログに残す。2026-08-24 の障害時、Streamlit Cloud Secrets
        # だけ古い値が残っていて手動生成と定時実行で別モデルが動き、原因が見えにくかった。
        if GEMINI_MODEL_IS_OVERRIDDEN:
            logger.warning(
                f"Gemini クライアント初期化: {self.model_name}"
                f"（環境変数 GEMINI_MODEL による上書き。リポジトリ既定は {GEMINI_MODEL_DEFAULT}。"
                f"Streamlit Cloud Secrets に古い値が残っていないか確認すること）"
            )
        else:
            logger.info(f"Gemini クライアント初期化: {self.model_name}（リポジトリ既定）")

    def _build_model(self, model_name: str) -> genai.GenerativeModel:
        return genai.GenerativeModel(
            model_name=model_name,
            system_instruction=self._system_instruction,
        )

    def _model_chain(self) -> list[str]:
        """既定モデル → 退避モデルの順（重複除去）。前から順に試す。"""
        chain = [self.model_name]
        for name in GEMINI_FALLBACK_MODELS:
            if name not in chain:
                chain.append(name)
        return chain

    @staticmethod
    def _classify_error(message: str) -> str:
        """
        例外メッセージを対処方針で分類する。

        - daily_quota: 日次枠(RPD)の枯渇。待っても回復しないので即モデル切替。
        - rate_limit : 分次レート(RPM)超過。65秒待てば回復する。
        - api        : 504/503 等のサーバ側都合。同一モデルで数回リトライする価値がある。
        - other      : JSONパース失敗・スキーマ検証エラー等。モデルを変えても直らない。
        """
        lowered = message.lower()
        is_quota = (
            "429" in message
            or "resource_exhausted" in lowered
            or "exceeded your current quota" in lowered
        )
        if is_quota:
            # quota_id 例: GenerateRequestsPerDayPerProjectPerModel-FreeTier
            return "daily_quota" if "perday" in lowered.replace("_", "") else "rate_limit"
        if any(token in lowered for token in ("504", "503", "500", "deadline", "unavailable", "timeout")):
            return "api"
        return "other"

    def generate_report(
        self,
        target_date: str,
        today_summary: dict,
        weekly_df,
        anomalies: list,
        extra_news: str = "",
        auto_fetch_news: bool | None = None,
        quality_feedback: str = "",
    ) -> tuple[ReadingReport, str]:
        """
        レポートを生成する。

        Returns:
            (ReadingReport Pydanticオブジェクト, 生のMarkdown文字列)
        """
        user_prompt = build_user_prompt(
            target_date,
            today_summary,
            weekly_df,
            anomalies,
            extra_news,
            auto_fetch_news=auto_fetch_news,
            quality_feedback=quality_feedback,
        )

        chain = self._model_chain()
        started = time.monotonic()
        quota_exhausted: set[str] = set()
        last_error: Exception | None = None

        for round_no in range(1, self.max_rounds + 1):
            candidates = [m for m in chain if m not in quota_exhausted]
            if not candidates:
                break
            if round_no > 1:
                if not self._fits_budget(started, extra_sec=GEMINI_ROUND_WAIT_SEC):
                    logger.warning("時間予算内にもう一巡できないため、ここで打ち切る")
                    break
                logger.warning(
                    f"全モデルが一時エラーで失敗。{GEMINI_ROUND_WAIT_SEC}秒待って"
                    f" {round_no}巡目に入る（混雑が引くのを待つ）"
                )
                time.sleep(GEMINI_ROUND_WAIT_SEC)
            # 2巡目以降は各モデル1回だけ。同じモデルを秒単位で叩き直しても混雑は引かない。
            attempts = self.MAX_RETRIES if round_no == 1 else 1

            for model_name in candidates:
                if model_name != self.model_name:
                    logger.warning(f"モデル切替: {self.model_name} → {model_name}")
                    self.model_name = model_name
                    self._model = self._build_model(model_name)

                result, error, verdict = self._try_model(
                    model_name, user_prompt, target_date, attempts, started
                )
                if result is not None:
                    return result
                last_error = error or last_error
                if verdict in ("give_up", "out_of_time"):
                    raise last_error if last_error else RuntimeError(
                        "Gemini レポート生成の時間予算を使い切りました"
                    )
                if verdict == "quota":
                    quota_exhausted.add(model_name)

        raise last_error if last_error else RuntimeError("Gemini レポート生成に失敗しました")

    @staticmethod
    def _fits_budget(started: float, extra_sec: float = 0) -> bool:
        """extra_sec 待ってから1リクエストを始めても、時間予算に収まるか。"""
        elapsed = time.monotonic() - started
        return elapsed + extra_sec + GEMINI_REQUEST_TIMEOUT_SEC <= GEMINI_TOTAL_BUDGET_SEC

    def _try_model(
        self,
        model_name: str,
        user_prompt: str,
        target_date: str,
        attempts: int,
        started: float,
    ) -> tuple[tuple[ReadingReport, str] | None, Exception | None, str]:
        """
        1モデルを最大 attempts 回試す。

        Returns:
            (成功時は (report_obj, markdown)・失敗時は None, 最後の例外, 判定)。判定は
            "ok" / "quota"（日次枠切れ。このモデルは今日もう使わない）/
            "transient"（503 等の一時エラー。次のモデルへ）/
            "give_up"（パース失敗等。モデルを変えても直らない）/
            "out_of_time"（時間予算切れ。job ごと打ち切られる前に止める）
        """
        last_error: Exception | None = None
        for attempt in range(attempts):
            if not self._fits_budget(started):
                logger.warning(f"時間予算が尽きるため {model_name} の呼び出しを始めない")
                return None, last_error, "out_of_time"
            try:
                logger.info(
                    f"Gemini API呼び出し中... (model={model_name} attempt {attempt + 1})"
                )
                response = self._model.generate_content(
                    user_prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.3,    # 分析の一貫性を重視
                        # 8192 では大きなレポートJSONが途中で切れて json.loads に失敗するため拡大
                        max_output_tokens=32768,
                        response_mime_type="application/json",
                    ),
                    # SDK 既定の retry は 429/504 を一時エラーとみなし、既定デッドライン
                    # 600秒のあいだ内部でリクエストを投げ直す。内部リトライ1回ごとに
                    # 日次枠(RPD)を消費するため、遅いモデルに当たると1回の呼び出しで
                    # 20 req/日を使い切る（2026-08-24 に 3.7-flash で実際に発生し、
                    # 直後の定時実行が 429 で全滅して AIレポートが欠落した）。
                    # 内部リトライを切り、「1呼び出し = 1リクエスト」を保証する。
                    request_options={
                        "retry": None,
                        "timeout": GEMINI_REQUEST_TIMEOUT_SEC,
                    },
                )

                raw_text = response.text
                logger.info(f"Gemini レスポンス受信: {len(raw_text)}文字")

                # JSONパース
                report_obj = self._parse_response(raw_text)

                # Markdown形式にレンダリング
                markdown = self._render_markdown(report_obj, target_date)
                lint_issues = lint_report_markdown(markdown, input_text=user_prompt)
                if lint_issues:
                    logger.warning(
                        "AIレポートlint警告: "
                        + " / ".join(issue.message for issue in lint_issues[:5])
                    )

                return (report_obj, markdown), None, "ok"

            except Exception as e:
                last_error = e
                kind = self._classify_error(str(e))
                logger.error(
                    f"Gemini APIエラー (model={model_name} "
                    f"attempt {attempt + 1}/{attempts}, 種別={kind}): {e}"
                )

                if kind == "daily_quota":
                    # RPD はモデル単位の枠で、リセットは太平洋時間の深夜＝JST 16:00。
                    # 同じモデルへの再試行は枠を削るだけなので待たずに切り替える。
                    logger.warning(
                        f"{model_name} の日次クォータ枯渇。リトライせず次のモデルへ移る。"
                    )
                    return None, e, "quota"

                if attempt == attempts - 1:
                    # パース失敗等はモデルを変えても直らないので、ここで打ち切る。
                    return None, e, ("give_up" if kind == "other" else "transient")

                # 無料枠は 5リクエスト/分。分次レートの 429 を秒単位でリトライすると
                # 同じ分内で再衝突して全滅するため、枠がリセットされる 60秒超を待つ
                # （2026-06-26〜07-02 の連続失敗の教訓）。
                wait = 65 if kind == "rate_limit" else 2 ** attempt
                logger.info(
                    f"{wait}秒後にリトライ..."
                    f"{'（レート枠リセット待ち）' if kind == 'rate_limit' else ''}"
                )
                time.sleep(wait)

        return None, last_error, "transient"

    @classmethod
    def _parse_response(cls, raw_text: str) -> ReadingReport:
        """JSON レスポンスをパースしてPydanticオブジェクトに変換。

        欠けた欄の既定値はスキーマ側（output_schema.py）が持つ。モデルが一部の欄を
        落としてもレポート生成は止めず、「未生成」と表示して品質チェックで拾う。
        """
        text = cls._extract_json_text(raw_text)

        try:
            data = cls._loads_json_tolerant(text)
        except json.JSONDecodeError as e:
            logger.error(f"JSONパースエラー: {e}\n{text[:500]}")
            raise ValueError(f"Geminiの出力がJSON形式ではありません: {e}")
        return ReadingReport(**data)

    @staticmethod
    def _loads_json_tolerant(text: str) -> dict:
        """JSONをパースする。厳密に失敗した場合は json-repair で修復して再パースする。

        gemini-3.5-flash は大きな日本語レポートで、文字列値内の未エスケープ引用符・
        改行・末尾カンマ等を含む壊れたJSONをときどき返す。json-repair で機械修復する。
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            from json_repair import repair_json

            repaired = repair_json(text)
            logger.warning("JSONが不正だったため json-repair で修復して再パースしました")
            return json.loads(repaired)

    @staticmethod
    def _extract_json_text(raw_text: str) -> str:
        """Geminiの応答からJSONオブジェクト部分だけを取り出す"""
        text = raw_text.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and start < end:
            return text[start:end + 1]

        return text

    def _render_markdown(self, report: ReadingReport, date: str) -> str:
        """Pydanticオブジェクトをレポート用Markdownに変換（描画は report_renderer に集約）"""
        return render_report_markdown(report, date)
