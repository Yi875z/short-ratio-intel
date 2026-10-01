"""
scripts/import_agent_report.py

外部のAIエージェント（ChatGPT の dots など、サブスク契約で動くもの）が書いたレポートJSONを
検証して ai_reports に保存する。

なぜこの形か（2026-10-01）:
    Gemini の無料枠が混雑で約36時間通らず、レポートが欠落した。有料APIは「レポートのたびに費用が
    かかる」ので避け、サブスク契約のエージェントに書かせる。エージェントには DB の資格情報を渡さず、
    受け渡しは Google Drive の専用フォルダだけにする。エージェントは差し替え可能な部品で、
    検証と保存はこのスクリプト（システム側）が持つ。検証に通らない出力は画面に載せない。

検証（Gemini 経路と同じ基準）:
    1. JSON として読めて ReadingReport に合う
    2. 中身がある（本日の結論があり、注目業種が3件以上）
    3. 表現lint と品質採点を記録する（high があっても保存はするが、ログと戻り値に出す）

使い方:
    python -m scripts.import_agent_report --date 2026-09-29 --file <report.json> --input <input.md> --dry-run
    python -m scripts.import_agent_report --date 2026-09-29 --file <report.json> --model chatgpt-dots
    python -m scripts.import_agent_report --from-drive                 # 本番（DB 最新日・Drive から）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

from loguru import logger

from src.ai_engine.gemini_client import _MIN_TOP_SECTORS, GeminiReportGenerator
from src.ai_engine.report_lint import lint_report_markdown
from src.ai_engine.report_quality import evaluate_report_quality
from src.ai_engine.report_renderer import render_report_markdown
from src.storage.db import save_ai_report


class AgentReportRejected(ValueError):
    """検証に通らなかった（画面に載せない）。"""


def validate_agent_report(raw_text: str, report_date: str, input_text: str = ""):
    """エージェントの出力を検証し、(ReadingReport, markdown, lint, quality) を返す。

    Raises:
        AgentReportRejected: JSON として読めない、または中身が空に近い
    """
    try:
        report = GeminiReportGenerator._parse_response(raw_text)
    except Exception as exc:  # noqa: BLE001 形式不正はすべて不合格として扱う
        raise AgentReportRejected(f"JSON として読めない: {exc}") from exc

    if not (report.executive_summary or "").strip():
        raise AgentReportRejected("本日の結論（executive_summary）が空")
    if len(report.top_sectors_analysis) < _MIN_TOP_SECTORS:
        raise AgentReportRejected(f"注目業種が {len(report.top_sectors_analysis)} 件しかない")

    markdown = render_report_markdown(report, report_date)
    # 材料（エージェントに渡した入力）と照合しないと、入力にあった SOX・WTI 等を
    # 「入力にない市場データ」と誤検知する
    lint = lint_report_markdown(markdown, input_text=input_text)
    quality = evaluate_report_quality(markdown, report.model_dump_json(), input_text=input_text)
    return report, markdown, lint, quality


def main() -> int:
    parser = argparse.ArgumentParser(description="外部エージェントのレポートJSONを検証して保存する")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（--from-drive では省略時 DB 最新日）")
    parser.add_argument("--file", default=None, help="エージェントが書いた JSON ファイル（手元）")
    parser.add_argument("--from-drive", action="store_true",
                        help="Google Drive の受け渡し用フォルダから読む（本番。材料も Drive から照合に使う）")
    parser.add_argument("--input", default="", help="エージェントに渡した材料ファイル（lint の照合用）")
    parser.add_argument("--model", default="chatgpt-dots", help="ai_reports.model_used に記録する名前")
    parser.add_argument("--dry-run", action="store_true", help="検証だけして保存しない")
    args = parser.parse_args()

    if args.from_drive:
        from src.storage import drive_exchange
        from src.storage.db import get_latest_date

        args.date = args.date or get_latest_date()
        try:
            raw = drive_exchange.fetch_agent_output(args.date)
            folders = drive_exchange.ensure_agent_folders()
            input_text = drive_exchange.download_text(
                folders.inputs, drive_exchange.input_name(args.date)
            ) or ""
        except drive_exchange.DriveNotConfigured:
            logger.warning("Google Drive の資格情報が未設定のため取り込まない")
            return 0
        if raw is None:
            # 未記入はエラーにしない（その日は機械版・Gemini 版のまま。自己点検で気づける）
            logger.warning(f"{args.date} のエージェント出力はまだ空（dots が未記入）。取り込まない")
            return 0
    else:
        if not (args.file and args.date):
            parser.error("--file と --date を指定するか、--from-drive を使う")
        raw = Path(args.file).read_text(encoding="utf-8-sig")
        input_text = Path(args.input).read_text(encoding="utf-8") if args.input else ""
    try:
        report, markdown, lint, quality = validate_agent_report(raw, args.date, input_text)
    except AgentReportRejected as exc:
        logger.error(f"不合格のため保存しない: {exc}")
        return 1

    logger.info(
        f"検証OK: {len(markdown):,}字 / 注目業種 {len(report.top_sectors_analysis)} / "
        f"lint {len(lint)}件 / 品質 {quality.status_label} {quality.score_pct}"
    )
    for issue in lint:
        logger.warning(f"lint [{issue.severity}] {issue.code}: {issue.line[:100]}")

    if args.dry_run:
        logger.info("--dry-run のため保存しない")
        return 0

    save_ai_report(
        args.date,
        report.current_macro_context,
        markdown,
        report_json=report.model_dump_json(),
        model_used=args.model,
    )
    logger.success(f"{args.date} のAIレポートを保存しました（model_used={args.model}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
