"""
パイプラインの自己点検。

## なぜこれが要るか

このプロジェクトは「静かに壊れる」ことで痛い目を見てきた。

- 2026-08: 取得元の表記変更でスクレイパーが0件を返し、3営業日ぶん欠測した。
  後段はDBの既存データで走り切るため、GitHub Actions は success で終わっていた。
- 2026-07-03 の引き継ぎメモに「H2に日本GDP・短観12月の公式日が出たら追記」と
  書かれていたが、2026-09-01 時点で `JP_GDP_2026` は2月と5月しか入っていない。
  8月中旬の1次速報はカレンダーから抜けたまま通過し、誰も気づかなかった。

共通するのは**警告が出ないこと**。人間の記憶や引き継ぎ文書に依存した宿題は落ちる。
落ちてはいけないものは、機械が毎日確認して鳴らす。

このモジュールは判定も修復もしない。「いま何がおかしいか」を文字列で返すだけで、
日次パイプラインの通知に載せる。fail-soft（点検自体の失敗で本処理を止めない）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

# キュレーション済みカレンダーが「この先これだけの日数はカバーしていてほしい」下限。
# 日次指標は先の日程が公表され次第の追記が必要で、放置すると静かに穴が空く。
_CALENDAR_HORIZON_DAYS = 60

# 保存済みデータがこれ以上古かったら鳴らす（営業日ではなくカレンダー日で見る）。
_STALE_DAYS = {
    "空売り比率": 5,
    "騰落銘柄数": 5,
    "業種別フロー特徴量": 5,
}


@dataclass(frozen=True)
class HealthIssue:
    """点検で見つかった不整合。severity は通知の並び順にだけ使う。"""

    severity: str      # "high" | "medium"
    area: str
    message: str
    action: str = ""

    def to_line(self) -> str:
        mark = "🔴" if self.severity == "high" else "🟡"
        line = f"{mark} [{self.area}] {self.message}"
        return f"{line} → {self.action}" if self.action else line


def check_calendar_coverage(
    today: Optional[date] = None,
    horizon_days: int = _CALENDAR_HORIZON_DAYS,
) -> list[HealthIssue]:
    """キュレーション済み経済カレンダーの「先が尽きている」カテゴリを検出する。

    カテゴリ×地域ごとに最終登録日を見て、今日から horizon_days 以内で尽きるなら鳴らす。
    公表日が未発表で埋められない場合もあるが、その場合も「未確認のまま尽きている」と
    分かるほうが、黙って抜けるより良い。
    """
    from config.market_calendar import CURATED_EVENTS

    today = today or date.today()
    deadline = today + timedelta(days=horizon_days)

    latest: dict[tuple[str, str], str] = {}
    for iso, _name, category, region, _importance, _note in CURATED_EVENTS:
        key = (category, region)
        if iso > latest.get(key, ""):
            latest[key] = iso

    issues: list[HealthIssue] = []
    for (category, region), last_iso in sorted(latest.items()):
        try:
            last_date = datetime.strptime(last_iso, "%Y-%m-%d").date()
        except ValueError:
            continue
        if last_date >= deadline:
            continue
        remaining = (last_date - today).days
        issues.append(HealthIssue(
            severity="high" if remaining < 0 else "medium",
            area="カレンダー",
            message=(
                f"{region}/{category} の登録が {last_iso} で尽きています"
                + ("（既に過去）" if remaining < 0 else f"（残り{remaining}日）")
            ),
            action="config/market_calendar.py に公式公表日を追記",
        ))
    return issues


def check_data_freshness(
    latest_dates: dict[str, Optional[str]],
    today: Optional[date] = None,
) -> list[HealthIssue]:
    """保存済みデータの最終日が古すぎないかを見る。

    Args:
        latest_dates: {"空売り比率": "2026-08-31", ...}。None は未取得。
    """
    today = today or date.today()
    issues: list[HealthIssue] = []

    for label, latest in latest_dates.items():
        limit = _STALE_DAYS.get(label, 5)
        if not latest:
            issues.append(HealthIssue(
                severity="high", area="データ鮮度",
                message=f"{label} が1件も保存されていません",
            ))
            continue
        try:
            latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - latest_date).days
        if age > limit:
            issues.append(HealthIssue(
                severity="high", area="データ鮮度",
                message=f"{label} の最終保存が {latest}（{age}日前）",
                action="取得経路が壊れていないか確認",
            ))
    return issues


def check_validation_staleness(
    last_run_iso: Optional[str],
    today: Optional[date] = None,
    interval_days: int = 180,
) -> list[HealthIssue]:
    """検証レポートが古くなっていないかを見る。

    「半年後に再検証する」という宿題を人間の記憶に預けないための仕掛け。
    月次ワークフローが動いていれば鳴らないので、鳴ったらワークフロー側が
    止まっているということでもある。
    """
    today = today or date.today()
    if not last_run_iso:
        return [HealthIssue(
            severity="medium", area="検証",
            message="業種別フロー特徴量の検証レポートが未生成です",
            action="python -m scripts.validate_sector_features",
        )]

    try:
        last = datetime.strptime(last_run_iso, "%Y-%m-%d").date()
    except ValueError:
        return []

    age = (today - last).days
    if age > interval_days:
        return [HealthIssue(
            severity="medium", area="検証",
            message=f"検証レポートが {last_run_iso}（{age}日前）のままです",
            action="python -m scripts.validate_sector_features で再評価",
        )]
    return []


def collect_health_issues(today: Optional[date] = None) -> list[HealthIssue]:
    """日次パイプラインから呼ぶ入口。点検の失敗で本処理を止めない。"""
    issues: list[HealthIssue] = []

    try:
        issues.extend(check_calendar_coverage(today))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"カレンダー点検に失敗（処理は継続）: {exc}")

    try:
        from src.storage.db import (
            get_latest_date,
            get_market_breadth_latest_date,
            get_saved_sector_feature_dates,
        )

        feature_dates = get_saved_sector_feature_dates()
        issues.extend(check_data_freshness({
            "空売り比率": get_latest_date(),
            "騰落銘柄数": get_market_breadth_latest_date(),
            "業種別フロー特徴量": feature_dates[0] if feature_dates else None,
        }, today))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"鮮度点検に失敗（処理は継続）: {exc}")

    try:
        issues.extend(check_validation_staleness(_read_validation_date(), today))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"検証レポート点検に失敗（処理は継続）: {exc}")

    order = {"high": 0, "medium": 1}
    return sorted(issues, key=lambda i: (order.get(i.severity, 9), i.area))


def format_health_block(issues: list[HealthIssue], limit: int = 8) -> str:
    """通知に載せる短いブロックを返す。問題が無ければ空文字。"""
    if not issues:
        return ""
    lines = [f"⚠️ 自己点検: {len(issues)}件"]
    lines += [f"  {issue.to_line()}" for issue in issues[:limit]]
    if len(issues) > limit:
        lines.append(f"  （ほか{len(issues) - limit}件）")
    return "\n".join(lines)


def _read_validation_date() -> Optional[str]:
    """検証レポートの生成日を本文から読む（ファイルが無ければ None）。"""
    import re
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "docs" / "validation_phase1.md"
    if not path.exists():
        return None
    matched = re.search(r"生成日時:\s*(\d{4}-\d{2}-\d{2})", path.read_text(encoding="utf-8"))
    return matched.group(1) if matched else None
