"""
scripts/notify_validation_summary.py

月次検証レポートの要点を Slack へ通知する。

⚠️ 解釈も判定も行わない。効いていない特徴量があれば「効いていない」とそのまま出す。
   通知が「良い知らせ」だけを運ぶようになった時点で、この仕組みは死ぬ。

なぜ通知するか: レポートをリポジトリに置くだけでは読まれない。
2026-07-03 の引き継ぎメモに書かれた宿題が2ヶ月放置された実績があるとおり、
文書は「気づける状態」を作るだけで「気づく」ことは保証しない。
毎日見ている Slack に相乗りさせるほうが確実に届く。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import requests
from loguru import logger

from config.settings import SLACK_WEBHOOK_URL

_REPORT_PATH = _PROJECT_ROOT / "docs" / "validation_phase1.md"

# スプレッドがこの幅に収まっていれば、実務上は差が無いと読む。
# レポート本文の基準と揃えてある。
_NOISE_BAND_PT = 0.1


def _parse_rows(markdown: str) -> list[dict]:
    """レポートの表から行を拾い、どの節・どのホライズンかを併せて持つ。

    節を区別するのが要点。条件付き標本（空売り比率Zスコア≥+1.0）は n≈400 と小さく、
    ホライズン間で符号が反転する。これを全営業日の結果と同列に数えると、
    毎月「差が出た」と鳴り続けてオオカミ少年になる。
    """
    rows: list[dict] = []
    horizon = None
    section = None

    for line in markdown.splitlines():
        matched = re.match(r"^## T\+(\d+)", line)
        if matched:
            horizon = int(matched.group(1))
            continue
        if line.startswith("### "):
            section = "full" if "全営業日" in line else "conditional"
            continue
        if not line.startswith("| ") or "---" in line:
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6 or cells[0] == "特徴量":
            continue
        spread = re.search(r"([+-]?\d+\.\d+)pt", cells[5])
        if not spread:
            continue

        rows.append({
            "feature": cells[0],
            "ic": cells[1],
            "t": _to_float(cells[2]),
            "spread": float(spread.group(1)),
            "horizon": horizon,
            "section": section,
        })
    return rows


def _to_float(text: str):
    try:
        return float(text.replace("+", ""))
    except (TypeError, ValueError):
        return None


def _significant(rows: list[dict]) -> list[dict]:
    """「効いている」と呼んでよい行だけを残す。

    条件は3つすべて。全営業日の標本であること、スプレッドがノイズ帯を超えること、
    t値が2以上あること。t値が出ていない（サンプル不足）行は数えない。
    """
    return [
        r for r in rows
        if r["section"] == "full"
        and abs(r["spread"]) >= _NOISE_BAND_PT
        and r["t"] is not None
        and abs(r["t"]) >= 2.0
    ]


def _sign_consistent(rows: list[dict], feature: str) -> bool:
    """全営業日の各ホライズンでスプレッドの符号が揃っているか。"""
    spreads = [
        r["spread"] for r in rows
        if r["feature"] == feature and r["section"] == "full"
    ]
    return len(spreads) >= 2 and (all(s > 0 for s in spreads) or all(s < 0 for s in spreads))


def build_summary() -> str:
    if not _REPORT_PATH.exists():
        return "⚠️ 月次検証: レポートが生成されていません（scripts/validate_sector_features を確認）"

    markdown = _REPORT_PATH.read_text(encoding="utf-8")
    period = re.search(r"対象期間:\s*(\S+)\s*〜\s*(\S+)", markdown)
    observations = re.search(r"観測数:\s*([\d,]+)行", markdown)
    rows = _parse_rows(markdown)

    if not rows:
        return "⚠️ 月次検証: レポートを読めませんでした（書式が変わった可能性）"

    full_rows = [r for r in rows if r["section"] == "full"]
    significant = _significant(rows)
    consistent = [r for r in significant if _sign_consistent(rows, r["feature"])]

    lines = ["📊 業種別フロー特徴量 月次検証"]
    if period:
        lines.append(f"・対象期間: {period.group(1)} 〜 {period.group(2)}")
    if observations:
        lines.append(f"・観測数: {observations.group(1)}行")
    lines.append(
        f"・有意な項目（全営業日・|t|≥2・|スプレッド|≥{_NOISE_BAND_PT}pt）: "
        f"{len(significant)} / {len(full_rows)}"
    )

    if full_rows:
        strongest = max(full_rows, key=lambda r: abs(r["t"] or 0))
        lines.append(
            f"・最大t値: {strongest['feature']} t={strongest['t']} "
            f"(スプレッド {strongest['spread']:+.3f}pt)"
        )

    if not significant:
        lines.append("→ 有意な予測力なし。状態分類を作らない判断を維持する。")
    elif not consistent:
        lines.append(
            "→ 有意な項目はあるが、ホライズン間で符号が反転しているためノイズの疑い。"
            "レポート本文を確認すること。"
        )
    else:
        names = "、".join(sorted({r["feature"] for r in consistent}))
        lines.append(
            f"→ 符号が一貫した有意項目あり: {names}。"
            "多重検定を踏まえたうえで、追試の価値があるか検討すること。"
        )
    return "\n".join(lines)


def main() -> None:
    summary = build_summary()
    logger.info(summary)

    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL 未設定のため通知をスキップ")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": summary}, timeout=15)
        resp.raise_for_status()
        logger.success("Slack へ通知しました")
    except Exception as exc:  # noqa: BLE001 通知失敗で異常終了させない
        logger.warning(f"Slack 通知に失敗: {exc}")


if __name__ == "__main__":
    main()
