"""
米国ショートフロー 日次レポート生成（US-P2）

AIを使わないルールベース生成。Gemini のクォータを消費しない。
AIレポート（日本側の夕方レポート）への文脈注入は US-P3 で行う。

⚠️ 出力文言に確定表現を入れないこと。すべて「候補」として書く（QCルール4）。
"""
from typing import Optional

import pandas as pd

from config.settings import US_ZSCORE_ALERT_THRESHOLD
from config.us_universe import ETF_THEME, TICKER_GROUP, US_UNIVERSE
from src.analyzer.us_basket import build_all_basket_metrics, compute_divergence
from src.analyzer.us_flow_analyzer import build_flow_metrics
from src.analyzer.us_flow_classifier import (
    PATTERN_LABELS,
    classify_flow_metrics,
    summarize_patterns,
)

# ETF乖離を見るときに構成銘柄側として使うバスケット
_DIVERGENCE_BASKET = "SEMI20"

# レポートは対象日しか表示しないため、指標計算は直近N営業日だけで足りる。
# SQUEEZE_BUILDING の連続3営業日判定に余裕を持たせた値。
_RECENT_ROWS_FOR_REPORT = 10


def _fmt(value: Optional[float], digits: int = 2, suffix: str = "", sign: bool = False) -> str:
    """欠損を「N/A」で表す。判定できなかったことを数値で誤魔化さない。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "N/A"
    fmt = f"{{:+.{digits}f}}" if sign else f"{{:.{digits}f}}"
    return fmt.format(float(value)) + suffix


def _pct(value: Optional[float]) -> str:
    return _fmt(value, digits=2, suffix="%")


def _return_pct(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "N/A"
    return f"{float(value) * 100:+.2f}%"


def build_daily_report(
    target_date: str,
    short_df: pd.DataFrame,
    price_df: Optional[pd.DataFrame] = None,
    universe: Optional[list[str]] = None,
) -> dict:
    """指定日の日次レポートを組み立てる。

    Args:
        target_date: 対象営業日 YYYY-MM-DD
        short_df:    米国ショートボリュームの全履歴（Zスコア算出に過去が必要）
        price_df:    日足OHLCV（任意）

    Returns:
        date / markdown / highlights / alerts / baskets / divergences / coverage を含む dict
    """
    expected = universe or US_UNIVERSE

    if short_df is None or short_df.empty:
        return _empty_report(target_date, expected)

    # 対象日以前に絞る。過去日を指定したときに未来の行が混ざらず、
    # tail_rows の「直近N営業日」も対象日を終端とする範囲になる。
    history = short_df[short_df["date"] <= target_date]
    if history.empty:
        return _empty_report(target_date, expected)

    metrics = classify_flow_metrics(
        build_flow_metrics(history, price_df, tail_rows=_RECENT_ROWS_FOR_REPORT)
    )
    today = metrics[metrics["date"] == target_date].copy()
    if today.empty:
        return _empty_report(target_date, expected)

    today = today.sort_values("z20", ascending=False, na_position="last")

    baskets = build_all_basket_metrics(history, target_date)
    divergences = [
        compute_divergence(history, _DIVERGENCE_BASKET, etf, target_date)
        for etf in ETF_THEME
    ]

    alerts = today[today["z20"].abs() >= US_ZSCORE_ALERT_THRESHOLD]
    pattern_counts = summarize_patterns(today)

    coverage = {
        "expected": len(expected),
        "present": int(today["ticker"].nunique()),
        "missing": sorted(set(expected) - set(today["ticker"])),
    }

    markdown = _render_markdown(
        target_date, today, baskets, divergences, alerts, pattern_counts, coverage
    )
    highlights = _render_highlights(target_date, baskets, divergences, alerts, coverage)

    return {
        "date": target_date,
        "markdown": markdown,
        "highlights": highlights,
        "alerts": alerts.to_dict("records"),
        "baskets": baskets,
        "divergences": divergences,
        "pattern_counts": pattern_counts,
        "coverage": coverage,
        "metrics": today,
    }


def _empty_report(target_date: str, expected: list[str]) -> dict:
    markdown = (
        f"# US Short Flow Daily - {target_date} (FINRA CNMS)\n\n"
        "## 0. データ健全性\n\n"
        f"- 対象日: {target_date} ⚠️ データなし\n"
        "- FINRA の当日ファイルが未公開か、米国休場の可能性があります。\n"
    )
    return {
        "date": target_date,
        "markdown": markdown,
        "highlights": f"米国ショートフロー: {target_date} のデータなし（未公開または休場）",
        "alerts": [],
        "baskets": [],
        "divergences": [],
        "pattern_counts": {},
        "coverage": {"expected": len(expected), "present": 0, "missing": sorted(expected)},
        "metrics": pd.DataFrame(),
    }


def _render_markdown(
    target_date: str,
    today: pd.DataFrame,
    baskets: list[dict],
    divergences: list[dict],
    alerts: pd.DataFrame,
    pattern_counts: dict,
    coverage: dict,
) -> str:
    lines: list[str] = [
        f"# US Short Flow Daily - {target_date} (FINRA CNMS)",
        "",
        "## 0. データ健全性",
        "",
        f"- 対象日: {target_date}",
        f"- 取得銘柄: {coverage['present']} / {coverage['expected']}",
        f"- 欠損: {', '.join(coverage['missing']) if coverage['missing'] else 'なし'}",
        "- ⚠️ 本データは FINRA 報告分（Off-Exchange）のみで、米国市場全体ではありません。",
        "- ⚠️ 日次ショートボリュームはフローであり、空売り残高（Short Interest）ではありません。",
        "",
        "## 1. バスケット（ボリューム加重）",
        "",
    ]

    if baskets:
        lines += [
            "| バスケット | Ratio | z20 | z60 | pct60 | 前日比 | 構成 |",
            "|---|---|---|---|---|---|---|",
        ]
        for b in baskets:
            lines.append(
                f"| {b['basket']} | {_pct(b['ratio'])} | {_fmt(b['z20'], sign=True)} | "
                f"{_fmt(b['z60'], sign=True)} | {_fmt(b['pct60'], digits=1)} | "
                f"{_fmt(b['dod_change'], sign=True)}pt | "
                f"{b['members_present']}/{b['members_expected']} |"
            )
    else:
        lines.append("バスケットを算出できませんでした（構成銘柄のデータ不足）。")

    lines += ["", "## 2. ETF乖離（テーマヘッジか銘柄選別か）", ""]
    if divergences:
        lines += [
            "| ETF | ETF Ratio | ETF z20 | 構成銘柄 Ratio | 構成銘柄 z20 | Divergence |",
            "|---|---|---|---|---|---|",
        ]
        for d in divergences:
            lines.append(
                f"| {d['etf']} | {_pct(d['etf_ratio'])} | {_fmt(d['etf_z20'], sign=True)} | "
                f"{_pct(d['basket_ratio'])} | {_fmt(d['basket_z20'], sign=True)} | "
                f"**{_fmt(d['divergence'], sign=True)}** |"
            )
        lines.append("")
        for d in divergences:
            lines.append(f"- {d['etf']}: {d['interpretation']}")

    lines += ["", f"## 3. アラート（|z20| ≧ {US_ZSCORE_ALERT_THRESHOLD}）", ""]
    if alerts.empty:
        lines.append("該当なし。")
    else:
        lines += [
            "| Ticker | Ratio | z20 | z60 | pct60 | Return | CLV | 出来高比 | パターン候補 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for _, r in alerts.iterrows():
            lines.append(_render_row(r))

    lines += ["", "## 4. パターン集計", ""]
    if pattern_counts:
        for tag, count in sorted(pattern_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {PATTERN_LABELS.get(tag, tag)}（{tag}）: {count}銘柄")
    else:
        lines.append("集計できませんでした。")

    lines += [
        "",
        "## 5. 全銘柄",
        "",
        "| Ticker | グループ | Ratio | z20 | z60 | pct60 | Return | CLV | 出来高比 | パターン候補 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in today.iterrows():
        group = TICKER_GROUP.get(r["ticker"], "N/A")
        lines.append(_render_row(r, group=group))

    lines += [
        "",
        "## 6. 注記",
        "",
        "- 本レポートのパターンはすべて**候補**です。単日のフローで方向性を断定しません。",
        "- 比率は必ず同一ソース内（FINRA報告分の分子 ÷ FINRA報告分の分母）で算出しています。",
        "- 絶対水準ではなく、銘柄自身の過去分布に対する相対位置（z20 / z60 / pct60）で判断してください。",
        "- 持ち越しショートの確認には Short Interest（隔週の残高）が必要です。取り込みは US-P3 で対応予定。",
        "",
    ]
    return "\n".join(lines)


def _render_row(r, group: Optional[str] = None) -> str:
    cells = [r["ticker"]]
    if group is not None:
        cells.append(group)
    cells += [
        _pct(r.get("short_ratio_pct")),
        _fmt(r.get("z20"), sign=True),
        _fmt(r.get("z60"), sign=True),
        _fmt(r.get("pct60"), digits=1),
        _return_pct(r.get("daily_return")),
        _fmt(r.get("clv"), sign=True),
        _fmt(r.get("volume_ratio")),
        str(r.get("pattern", "")),
    ]
    return "| " + " | ".join(cells) + " |"


def _render_highlights(
    target_date: str,
    baskets: list[dict],
    divergences: list[dict],
    alerts: pd.DataFrame,
    coverage: dict,
) -> str:
    """Slack通知用の短い要約。"""
    parts = [f"🇺🇸 米国ショートフロー ({target_date}) 取得 {coverage['present']}/{coverage['expected']}銘柄"]

    for b in baskets:
        parts.append(f"・{b['basket']}: {_pct(b['ratio'])} (z20 {_fmt(b['z20'], sign=True)})")

    for d in divergences:
        if d["divergence"] is not None:
            parts.append(f"・{d['etf']}乖離 {_fmt(d['divergence'], sign=True)}: {d['interpretation']}")

    if alerts.empty:
        parts.append("・アラート: なし")
    else:
        top = alerts.head(5)
        names = ", ".join(
            f"{r['ticker']}({_fmt(r['z20'], sign=True)}/{r.get('pattern', '')})"
            for _, r in top.iterrows()
        )
        parts.append(f"・アラート{len(alerts)}件: {names}")

    return "\n".join(parts)
