"""
米国ショートフロー 日次レポート生成（US-P2）

AIを使わないルールベース生成。Gemini のクォータを消費しない。
AIレポート（日本側の夕方レポート）への文脈注入は US-P3 で行う。

⚠️ 出力文言に確定表現を入れないこと。すべて「候補」として書く（QCルール4）。
"""
from typing import Optional

import pandas as pd

from config.settings import US_ZSCORE_ALERT_THRESHOLD
from config.us_universe import DIVERGENCE_PAIRS, TICKER_GROUP, US_UNIVERSE
from src.analyzer.us_basket import (
    build_all_basket_metrics,
    build_all_basket_spreads,
    compute_divergence,
)
from src.analyzer.us_flow_analyzer import build_flow_metrics
from src.analyzer.us_flow_classifier import (
    PATTERN_LABELS,
    classify_flow_metrics,
    summarize_patterns,
)

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


def _z_phrase(z: Optional[float]) -> str:
    """Zスコアを日本語の水準表現にする。"""
    if z is None or pd.isna(z):
        return "過去との比較はまだできません（履歴不足）"
    z = float(z)
    if z >= 2.0:
        level = "過去20日と比べて際立って高い"
    elif z >= 1.0:
        level = "過去20日と比べてやや高い"
    elif z <= -2.0:
        level = "過去20日と比べて際立って低い"
    elif z <= -1.0:
        level = "過去20日と比べてやや低い"
    else:
        level = "過去20日並み"
    return f"{z:+.2f}σ（{level}）"


def _clv_phrase(clv: Optional[float]) -> str:
    """終値位置を日本語にする。"""
    if clv is None or pd.isna(clv):
        return ""
    clv = float(clv)
    if clv >= 0.3:
        return "高値引け寄り"
    if clv <= -0.3:
        return "安値引け寄り"
    return "中位引け"


def _volume_phrase(volume_ratio: Optional[float]) -> str:
    """出来高比を日本語にする。"""
    if volume_ratio is None or pd.isna(volume_ratio):
        return ""
    volume_ratio = float(volume_ratio)
    if volume_ratio >= 1.2:
        return f"出来高は平常の{volume_ratio:.2f}倍（増加）"
    if volume_ratio <= 0.8:
        return f"出来高は平常の{volume_ratio:.2f}倍（減少）"
    return f"出来高は平常並み（{volume_ratio:.2f}倍）"


def describe_row(row) -> str:
    """1銘柄の状況を日本語1文で説明する。

    断定を避け、観測事実を並べたうえで「〜の候補」で締める（QCルール4）。
    """
    ticker = row.get("ticker", "")
    ratio = row.get("short_ratio_pct")
    parts = [f"ショート比率{_pct(ratio)}"]

    parts.append(_z_phrase(row.get("z20")))

    pct60 = row.get("pct60")
    if pct60 is not None and not pd.isna(pct60):
        parts.append(f"直近60日の分布では下から{float(pct60):.0f}%の位置")

    daily_return = row.get("daily_return")
    if daily_return is not None and not pd.isna(daily_return):
        move = f"株価{_return_pct(daily_return)}"
        clv = _clv_phrase(row.get("clv"))
        parts.append(f"{move}・{clv}" if clv else move)

    volume = _volume_phrase(row.get("volume_ratio"))
    if volume:
        parts.append(volume)

    label = PATTERN_LABELS.get(row.get("pattern", ""), "")
    body = "、".join(parts)
    return f"{ticker}: {body}。→ {label}" if label else f"{ticker}: {body}。"


def describe_day(report: dict) -> str:
    """その日の全体像を日本語の短い段落にする。"""
    baskets = report.get("baskets") or []
    divergences = report.get("divergences") or []
    alerts = report.get("alerts") or []

    lines: list[str] = []

    for b in baskets:
        if b["basket"] != "SEMI20":
            continue
        lines.append(
            f"半導体20銘柄をまとめたショート比率は{_pct(b['ratio'])}で、"
            f"{_z_phrase(b['z20'])}。"
        )

    for d in divergences:
        if d["divergence"] is None:
            continue
        lines.append(
            f"ETFの{d['etf']}と個別銘柄の差（乖離）は{d['divergence']:+.2f}。{d['interpretation']}。"
        )
        break   # 代表としてSMHのみ。SOXXは表で確認できる

    # 偏りが最も大きいペアだけを述べる（全ペアを並べると読みづらいため）
    notable = [s for s in (report.get("spreads") or []) if s["spread"] is not None]
    if notable:
        top = max(notable, key=lambda s: abs(s["spread"]))
        if abs(top["spread"]) > 1.5:
            lines.append(
                f"ペアで見ると「{top['name']}」の差が{top['spread']:+.2f}で最も開いています。"
                f"{top['interpretation']}。"
            )

    if alerts:
        names = "・".join(a["ticker"] for a in alerts[:5])
        lines.append(
            f"過去との差が大きい銘柄は{len(alerts)}件（{names}）。"
            "個別の読み方は下の一覧を参照してください。"
        )
    else:
        lines.append("過去との差が際立つ銘柄はありませんでした。")

    lines.append(
        "いずれも当日のフロー（売買の流れ）から見た候補であり、"
        "持ち越しの空売り残高が増えたことを示すものではありません。"
    )
    return "".join(lines)


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
    # ETFと、その対になる構成銘柄バスケットの組み合わせで乖離を見る
    divergences = [
        compute_divergence(history, basket, etf, target_date)
        for etf, basket in DIVERGENCE_PAIRS
    ]

    spreads = build_all_basket_spreads(history, target_date)

    alerts = today[today["z20"].abs() >= US_ZSCORE_ALERT_THRESHOLD]
    pattern_counts = summarize_patterns(today)

    coverage = {
        "expected": len(expected),
        "present": int(today["ticker"].nunique()),
        "missing": sorted(set(expected) - set(today["ticker"])),
    }

    markdown = _render_markdown(
        target_date, today, baskets, divergences, spreads, alerts, pattern_counts, coverage
    )
    highlights = _render_highlights(target_date, baskets, divergences, alerts, coverage)

    result = {
        "date": target_date,
        "markdown": markdown,
        "highlights": highlights,
        "alerts": alerts.to_dict("records"),
        "baskets": baskets,
        "divergences": divergences,
        "spreads": spreads,
        "pattern_counts": pattern_counts,
        "coverage": coverage,
        "metrics": today,
    }
    # 日本語の読み下し。Markdown・Streamlit の両方で同じ文面を使う（DRY）
    result["summary_ja"] = describe_day(result)
    result["alert_descriptions"] = [describe_row(r) for _, r in alerts.iterrows()]
    return result


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
        "spreads": [],
        "pattern_counts": {},
        "coverage": {"expected": len(expected), "present": 0, "missing": sorted(expected)},
        "metrics": pd.DataFrame(),
        "summary_ja": f"{target_date} は米国のデータがありません（FINRA未公開または休場）。",
        "alert_descriptions": [],
    }


def _render_markdown(
    target_date: str,
    today: pd.DataFrame,
    baskets: list[dict],
    divergences: list[dict],
    spreads: list[dict],
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
        "## 1. この日の読み方",
        "",
        describe_day({
            "baskets": baskets,
            "divergences": divergences,
            "spreads": spreads,
            "alerts": alerts.to_dict("records"),
        }),
        "",
        "## 2. バスケット（ボリューム加重）",
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

    lines += ["", "## 3. ペア比較（ロング候補 vs ショート候補）", ""]
    if spreads:
        lines += [
            "| 対 | ロング側 z20 | ショート側 z20 | 差 | 読み |",
            "|---|---|---|---|---|",
        ]
        for sp in spreads:
            lines.append(
                f"| {sp['name']} | {_fmt(sp['long_z20'], sign=True)} | "
                f"{_fmt(sp['short_z20'], sign=True)} | **{_fmt(sp['spread'], sign=True)}** | "
                f"{sp['interpretation']} |"
            )
        lines.append("")
        lines.append("差は空売り比率そのものではなく、各群が自分の過去分布からどれだけ離れたかの差です。")
    else:
        lines.append("算出できませんでした。")

    lines += ["", f"## 4. アラート（|z20| ≧ {US_ZSCORE_ALERT_THRESHOLD}）", ""]
    if alerts.empty:
        lines.append("該当なし。")
    else:
        for _, r in alerts.iterrows():
            lines.append(f"- {describe_row(r)}")
        lines += [
            "",
            "| 銘柄 | ショート比率 | 20日Zスコア | 60日Zスコア | 60日順位% | 騰落率 | 終値位置 | 出来高比 | パターン候補 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for _, r in alerts.iterrows():
            lines.append(_render_row(r))

    lines += ["", "## 5. パターン集計", ""]
    if pattern_counts:
        for tag, count in sorted(pattern_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {PATTERN_LABELS.get(tag, tag)}（{tag}）: {count}銘柄")
    else:
        lines.append("集計できませんでした。")

    lines += [
        "",
        "## 6. 全銘柄",
        "",
        "| 銘柄 | グループ | ショート比率 | 20日Zスコア | 60日Zスコア | 60日順位% | 騰落率 | 終値位置 | 出来高比 | パターン候補 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in today.iterrows():
        group = TICKER_GROUP.get(r["ticker"], "N/A")
        lines.append(_render_row(r, group=group))

    lines += [
        "",
        "## 7. 注記",
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
