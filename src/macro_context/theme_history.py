"""
市場テーマ判定の履歴比較ユーティリティ。
"""
from __future__ import annotations


def build_theme_comparison_rows(
    current_themes: list[dict],
    previous_themes: list[dict],
) -> list[dict]:
    """選択日と前回保存日のテーマスコアを比較する。"""
    current_by_key = {_theme_key(theme): theme for theme in current_themes}
    previous_by_key = {_theme_key(theme): theme for theme in previous_themes}
    all_keys = sorted(set(current_by_key) | set(previous_by_key))

    rows = []
    for key in all_keys:
        current = current_by_key.get(key)
        previous = previous_by_key.get(key)
        current_score = _score(current)
        previous_score = _score(previous)
        score_change = (
            round(current_score - previous_score, 2)
            if current is not None and previous is not None
            else None
        )

        if current is not None and previous is None:
            state = "新規"
        elif current is None and previous is not None:
            state = "消滅"
        elif score_change is not None and score_change >= 1.0:
            state = "強化"
        elif score_change is not None and score_change <= -1.0:
            state = "弱体化"
        else:
            state = "継続"

        theme = current or previous or {}
        rows.append({
            "key": key,
            "name": theme.get("name", key),
            "state": state,
            "current_score": current_score if current is not None else None,
            "previous_score": previous_score if previous is not None else None,
            "score_change": score_change,
            "current_status": current.get("status", "") if current else "",
            "previous_status": previous.get("status", "") if previous else "",
            "confidence": current.get("confidence", "") if current else previous.get("confidence", ""),
            "related_sectors": ", ".join(theme.get("related_sectors", [])),
            "unverified_count": len(theme.get("unverified_data", [])),
        })

    return sorted(rows, key=_comparison_sort_key)


def build_theme_history_rows(snapshots_by_date: dict[str, list[dict]]) -> list[dict]:
    """チャート・表で扱いやすい履歴行へ平坦化する。"""
    rows = []
    for date_value in sorted(snapshots_by_date):
        for theme in snapshots_by_date[date_value]:
            rows.append({
                "date": date_value,
                "key": _theme_key(theme),
                "name": theme.get("name", _theme_key(theme)),
                "score": _score(theme),
                "status": theme.get("status", ""),
                "confidence": theme.get("confidence", ""),
                "evidence_count": len(theme.get("evidence", [])),
                "related_sectors": ", ".join(theme.get("related_sectors", [])),
                "unverified_count": len(theme.get("unverified_data", [])),
            })
    return rows


def build_theme_transition_prompt_block(
    target_date: str,
    current_themes: list[dict],
    previous_themes: list[dict],
    previous_date: str | None = None,
    current_source: str = "saved",
    max_rows: int = 6,
) -> str:
    """AIプロンプトへ入れる市場テーマ転換メモを作る。"""
    lines = [
        "【市場テーマ履歴比較】",
        f"- 対象日: {target_date}",
        f"- 現在テーマの取得元: {current_source}",
        f"- 比較基準日: {previous_date if previous_date else '前回保存データなし'}",
        "- 注意: スコア差分はテーマ候補の強弱を示す補助情報であり、指数・金利・為替などの実測値ではない。",
    ]

    if not current_themes and not previous_themes:
        lines.extend([
            "- 保存済みテーマ履歴なし。今回入力された市場テーマ判定だけを使う。",
            "- `theme_shift_analysis` では、履歴比較ではなく今回入力内の根拠に限定して条件付きで書く。",
        ])
        return "\n".join(lines)

    rows = build_theme_comparison_rows(current_themes, previous_themes)
    if not rows:
        lines.append("- 比較可能なテーマ行なし。")
        return "\n".join(lines)

    lines.append("- テーマ変化:")
    for row in rows[:max_rows]:
        current_score = _format_optional_score(row.get("current_score"))
        previous_score = _format_optional_score(row.get("previous_score"))
        score_change = _format_score_change(row.get("score_change"))
        related = row.get("related_sectors") or "関連業種未特定"
        lines.append(
            f"  - [{row['state']}] {row['name']}: "
            f"今回{current_score} / 前回{previous_score} / 差分{score_change} / "
            f"今回状態={row.get('current_status') or 'N/A'} / 関連業種={related}"
        )

    lines.extend([
        "- 解釈指示:",
        "  - 新規・強化テーマは `dominant_market_themes` と `theme_shift_analysis` へ優先的に反映する。",
        "  - 弱体化・消滅テーマは、前提テーマの後退または市場の関心移動として条件付きで扱う。",
        "  - テーマ変化と業種別空売り比率・価格規制あり/なしが整合するかを `theme_sector_alignment` に明記する。",
    ])
    return "\n".join(lines)


def find_previous_theme_date(dates: list[str], selected_date: str) -> str | None:
    """選択日より前の直近テーマ保存日を返す。"""
    previous_dates = [date_value for date_value in dates if date_value < selected_date]
    if not previous_dates:
        return None
    return sorted(previous_dates)[-1]


def _theme_key(theme: dict) -> str:
    return str(theme.get("key") or theme.get("theme_key") or theme.get("name") or "")


def _score(theme: dict | None) -> float:
    if theme is None:
        return 0.0
    try:
        return round(float(theme.get("score", 0) or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _format_optional_score(value) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "N/A"


def _format_score_change(value) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):+.1f}"
    except (TypeError, ValueError):
        return "N/A"


def _comparison_sort_key(row: dict) -> tuple:
    state_rank = {
        "新規": 0,
        "強化": 1,
        "継続": 2,
        "弱体化": 3,
        "消滅": 4,
    }
    change = row.get("score_change")
    abs_change = abs(change) if change is not None else 999
    current_score = row.get("current_score") or 0
    previous_score = row.get("previous_score") or 0
    return (
        state_rank.get(row.get("state"), 9),
        -abs_change,
        -max(current_score, previous_score),
        row.get("name", ""),
    )
