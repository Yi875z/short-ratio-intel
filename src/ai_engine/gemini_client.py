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
)
from src.ai_engine.output_schema import ReadingReport
from src.ai_engine.prompt_builder import build_system_prompt, build_user_prompt
from src.ai_engine.report_lint import lint_report_markdown


class GeminiReportGenerator:
    """Gemini を使った空売り比率レポート生成クラス"""

    MAX_RETRIES = 3

    def __init__(self):
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

        last_error: Exception | None = None
        chain = self._model_chain()

        for index, model_name in enumerate(chain):
            if index > 0:
                logger.warning(f"退避モデルへ切替: {chain[index - 1]} → {model_name}")
                self.model_name = model_name
                self._model = self._build_model(model_name)

            move_to_next_model = False

            for attempt in range(self.MAX_RETRIES):
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

                    return report_obj, markdown

                except Exception as e:
                    last_error = e
                    kind = self._classify_error(str(e))
                    logger.error(
                        f"Gemini APIエラー (model={model_name} "
                        f"attempt {attempt + 1}/{self.MAX_RETRIES}, 種別={kind}): {e}"
                    )

                    if kind == "daily_quota":
                        # RPD はモデル単位の枠で、リセットは太平洋時間の深夜＝JST 16:00。
                        # 同じモデルへの再試行は枠を削るだけなので待たずに切り替える。
                        logger.warning(
                            f"{model_name} の日次クォータ枯渇。リトライせず次のモデルへ移る。"
                        )
                        move_to_next_model = True
                        break

                    if attempt == self.MAX_RETRIES - 1:
                        # パース失敗等はモデルを変えても直らないので、ここで打ち切る。
                        move_to_next_model = kind != "other"
                        break

                    # 無料枠は 5リクエスト/分。分次レートの 429 を秒単位でリトライすると
                    # 同じ分内で再衝突して全滅するため、枠がリセットされる 60秒超を待つ
                    # （2026-06-26〜07-02 の連続失敗の教訓）。
                    wait = 65 if kind == "rate_limit" else 2 ** attempt
                    logger.info(
                        f"{wait}秒後にリトライ..."
                        f"{'（レート枠リセット待ち）' if kind == 'rate_limit' else ''}"
                    )
                    time.sleep(wait)

            if not move_to_next_model:
                break

        raise last_error if last_error else RuntimeError("Gemini レポート生成に失敗しました")

    @classmethod
    def _parse_response(cls, raw_text: str) -> ReadingReport:
        """JSON レスポンスをパースしてPydanticオブジェクトに変換"""
        text = cls._extract_json_text(raw_text)

        try:
            data = cls._loads_json_tolerant(text)
            # モデルが新設フィールドを落とした場合でも、レポート生成を止めない。
            data.setdefault(
                "jpx_short_selling_breakdown_analysis",
                "JPX公式内訳の専用分析は未生成です。東証全体サマリーを参照してください。",
            )
            data.setdefault(
                "price_restriction_signal",
                "価格規制あり/なしの専用シグナルは未生成です。",
            )
            data.setdefault(
                "other_category_impact",
                "その他（33業種外）の専用分析は未生成です。",
            )
            data.setdefault(
                "signal_history_analysis",
                "シグナル履歴の専用分析は未生成です。",
            )
            data.setdefault(
                "persistent_signal_summary",
                "継続シグナルの専用分析は未生成です。",
            )
            data.setdefault(
                "new_signal_summary",
                "新規シグナルの専用分析は未生成です。",
            )
            data.setdefault(
                "faded_signal_summary",
                "消滅・弱体化シグナルの専用分析は未生成です。",
            )
            data.setdefault(
                "event_calendar_context",
                "市場イベント文脈の専用分析は未生成です。",
            )
            data.setdefault(
                "institutional_flow_alignment",
                "投資主体別フローの突合は未生成です。",
            )
            data.setdefault(
                "investment_guardrails",
                [
                    "本レポートは売買推奨ではなく、JPX日次フローを使った需給分析です。",
                    "空売り比率単独では判断せず、株価・出来高・先物・外部イベントを合わせて確認してください。",
                    "新規シグナルは初動候補であり、翌営業日の再現性確認を前提に扱ってください。",
                ],
            )
            data.setdefault(
                "confirmation_conditions",
                [
                    "方向性売り寄りの業種で価格規制あり比率が継続するか。",
                    "ショートカバー候補で総空売り比率と価格規制あり比率が同時に低下するか。",
                    "東証全体と業種別の乖離が縮小するか拡大するか。",
                ],
            )
            data.setdefault(
                "false_positive_risks",
                [
                    "価格規制なしの上昇はヘッジ・裁定・流動性供給を含むため、弱気売りと単純解釈しないでください。",
                    "その他（33業種外）の影響が大きい日は、33業種平均と市場全体がずれる可能性があります。",
                    "単日だけの急変はイベント起因の一過性フローである可能性があります。",
                ],
            )
            data.setdefault(
                "additional_data_to_check",
                [
                    "対象業種の株価推移と出来高。",
                    "TOPIX・日経平均先物、オプション、ボラティリティ指標。",
                    "主体別売買動向、信用残、個別銘柄のニュース。",
                ],
            )
            data.setdefault("executive_summary", "")
            data.setdefault("regime", "")
            data.setdefault("dominant_market_themes", [])
            data.setdefault(
                "theme_shift_analysis",
                "市場テーマ転換の専用分析は未生成です。",
            )
            data.setdefault(
                "theme_sector_alignment",
                "市場テーマと業種別空売り比率の整合性分析は未生成です。",
            )
            data.setdefault("unverified_market_data", [])
            return ReadingReport(**data)
        except json.JSONDecodeError as e:
            logger.error(f"JSONパースエラー: {e}\n{text[:500]}")
            raise ValueError(f"Geminiの出力がJSON形式ではありません: {e}")

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
        """Pydanticオブジェクトをレポート用Markdownに変換"""
        lines = [
            f"# 📊 空売り比率 完全解読レポート",
            f"## 〜 33業種分析×マクロ統合〜 {date}",
            "",
            "> 注: 本レポートの空売り比率はJPX日次売買代金フローであり、空売り残高・建玉ではありません。",
            "",
            "---",
            "",
        ]

        if report.executive_summary or report.regime:
            lines += ["## 🧭 本日の結論"]
            if report.executive_summary:
                lines += [report.executive_summary, ""]
            if report.regime:
                lines += [f"**レジーム判定**: {report.regime}", ""]
            lines += ["---", ""]

        lines += [
            f"## 🌍 現在の支配的マクロ背景",
            f"{report.current_macro_context}",
            "",
            "---",
            "",
            f"## 📈 東証全体サマリー",
            f"{report.market_overall_summary}",
            "",
            f"## 🧭 JPX空売り内訳分析",
            f"{report.jpx_short_selling_breakdown_analysis}",
            "",
            f"## ⚖️ 価格規制あり/なしの需給シグナル",
            f"{report.price_restriction_signal}",
            "",
            f"## 🧩 その他（33業種外）の影響",
            f"{report.other_category_impact}",
            "",
            f"## 🗓️ 市場イベント文脈",
            f"{report.event_calendar_context}",
            "",
            f"## 📅 週次トレンド解析",
            f"{report.weekly_trend_analysis}",
            "",
            f"## 🧭 市場テーマ判定",
        ]

        if report.dominant_market_themes:
            for theme in report.dominant_market_themes:
                lines += [
                    f"### {theme.theme_name}",
                    f"- **重要度**: {theme.importance}",
                    f"- **状態**: {theme.status}",
                    f"- **フロー区分**: {theme.flow_classification}",
                    f"- **影響経路**: {', '.join(theme.impact_channels)}",
                    f"- **関連業種**: {', '.join(theme.related_sectors)}",
                    f"- **空売り比率との整合性**: {theme.short_ratio_alignment}",
                    f"- **根拠**: {' / '.join(theme.evidence)}",
                    f"- **注記**: {theme.caveat}",
                    "",
                ]
        else:
            lines += ["市場テーマ判定は未生成です。", ""]

        lines += [
            "### テーマ転換シグナル",
            report.theme_shift_analysis,
            "",
            "### テーマと業種別空売りの整合性",
            report.theme_sector_alignment,
            "",
        ]
        if report.unverified_market_data:
            lines += ["### 未確認データ"]
            for item in report.unverified_market_data:
                lines.append(f"- {item}")
            lines.append("")

        lines += [
            f"## 🚨 シグナル履歴分析",
            f"{report.signal_history_analysis}",
            "",
            f"### 継続シグナル",
            f"{report.persistent_signal_summary}",
            "",
            f"### 新規シグナル",
            f"{report.new_signal_summary}",
            "",
            f"### 消滅・弱体化シグナル",
            f"{report.faded_signal_summary}",
            "",
            "---",
            "",
            "## 🛡️ 投資判断ガードレール",
            "",
            "### このレポートの使い方",
        ]
        for item in report.investment_guardrails:
            lines.append(f"- {item}")

        lines += [
            "",
            "### 翌営業日の確認条件",
        ]
        for item in report.confirmation_conditions:
            lines.append(f"- {item}")

        lines += [
            "",
            "### 誤判定しやすいケース",
        ]
        for item in report.false_positive_risks:
            lines.append(f"- {item}")

        lines += [
            "",
            "### 追加で見るべきデータ",
        ]
        for item in report.additional_data_to_check:
            lines.append(f"- {item}")

        lines += [
            "",
            "---",
            "",
            "## ⚔️ Retail Trap vs Pro Intent",
            "",
            f"**🪤 Retail Trap（素人の罠）**",
            f"{report.retail_trap}",
            "",
            f"**🎯 Pro Intent（機関の真の狙い）**",
            f"{report.pro_intent}",
            "",
            f"**🏦 投資主体別フローとの整合性**",
            f"{report.institutional_flow_alignment}",
            "",
            "---",
            "",
            "## 🔴 高空売りゾーン 注目業種",
        ]

        for s in report.top_sectors_analysis:
            lines += [
                f"### {s.sector_name}（{s.short_ratio_pct:.1f}%）",
                f"- **ゾーン**: {s.zone_label}",
                f"- **Pro Intent**: {s.pro_intent}",
                f"- **Retail Trap**: {s.retail_trap}",
                f"- **解釈**: {s.interpretation}",
                "",
            ]

        lines += ["---", "", "## 🟢 低空売りゾーン 注目業種"]
        for s in report.low_sectors_analysis:
            lines += [
                f"### {s.sector_name}（{s.short_ratio_pct:.1f}%）",
                f"- **解釈**: {s.interpretation}",
                "",
            ]

        if report.anomaly_commentary:
            lines += ["---", "", "## ⚠️ 異常値解説", report.anomaly_commentary, ""]

        lines += ["---", "", "## 💡 戦略的示唆"]
        for i, sg in enumerate(report.strategic_suggestions, 1):
            lines += [
                f"### {i}. {sg.title}",
                f"- **対象業種**: {', '.join(sg.target_sectors)}",
                f"- **戦略タイプ**: {sg.strategy_type}",
                f"- **根拠**: {sg.rationale}",
                f"- **リスク注意**: {sg.risk_warning}",
                "",
            ]

        lines += [
            "---",
            "",
            "## 📌 総括",
            report.overall_conclusion,
            "",
            "## 👁 次の監視ポイント",
        ]
        for wp in report.next_watch_points:
            lines.append(f"- {wp}")

        return "\n".join(lines)
