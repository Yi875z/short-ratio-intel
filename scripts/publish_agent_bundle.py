"""
scripts/publish_agent_bundle.py

サブスクの AI エージェント（dots）に渡す材料を Google Drive の受け渡し用フォルダへ置く。
日次パイプライン（daily_fetch.yml）の取得後に実行する。

fail-soft: Drive の資格情報が未設定、または配置に失敗してもパイプラインは止めない（終了コード0）。
その日は機械版（または Gemini 版）のレポートのままになり、自己点検の「AIレポート」で気づける。

使い方:
    python -m scripts.publish_agent_bundle                 # DB の最新日
    python -m scripts.publish_agent_bundle --date 2026-10-01
    python -m scripts.publish_agent_bundle --local-dir <フォルダ>   # Drive ではなく手元へ書く（試運転用）
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

from src.ai_engine.agent_bundle import build_agent_bundle, build_agent_instructions
from src.storage import drive_exchange
from src.storage.db import get_latest_date


def main() -> int:
    parser = argparse.ArgumentParser(description="dots 向けの材料を Google Drive へ置く")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は DB 最新日）")
    parser.add_argument("--local-dir", default=None, help="Drive ではなくこのフォルダへ書く（試運転用）")
    args = parser.parse_args()

    report_date = args.date or get_latest_date()
    if not report_date:
        logger.warning("DB にデータが無く対象日を決められないため、材料は置かない")
        return 0

    try:
        from scripts.fetch_short_ratio import _prepare_analysis

        _, today_summary, weekly_df, anomalies, _ = _prepare_analysis(report_date)
        bundle = build_agent_bundle(report_date, today_summary, weekly_df, anomalies)

        if args.local_dir:
            base = Path(args.local_dir)
            (base / "inputs").mkdir(parents=True, exist_ok=True)
            (base / "outputs").mkdir(parents=True, exist_ok=True)
            (base / "inputs" / drive_exchange.input_name(report_date)).write_text(bundle, encoding="utf-8")
            (base / "README_dots_task.md").write_text(
                build_agent_instructions(base.name), encoding="utf-8"
            )
            logger.info(f"手元へ材料を書いた: {base}")
            return 0

        drive_exchange.publish_bundle(
            report_date, bundle, build_agent_instructions(drive_exchange.AGENT_FOLDER_NAME)
        )
        logger.success(f"{report_date} の材料を Drive へ置いた（{len(bundle):,}字）")
    except drive_exchange.DriveNotConfigured:
        logger.warning("Google Drive の資格情報が未設定のため、dots 向けの材料は置かない")
    except Exception as exc:  # noqa: BLE001 受け渡しの失敗で日次パイプラインを止めない
        logger.error(f"dots 向けの材料の配置に失敗（パイプラインは継続）: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
