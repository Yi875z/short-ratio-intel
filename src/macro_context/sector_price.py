"""
東証33業種別株価指数の日次騰落率（フェーズ2-1）

空売り比率だけでは「高い＝弱気」に解釈が寄り、踏み上げや買い吸収を見分けられない。
株価の反応と突き合わせて4象限で読むための材料を供給する。

データ源: nikkei225jp.com が業種別株価ページ用に配信している履歴JS
    https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo_past.js
    形式: GY[q]="YYYY/MM/DD,HH:MM,<33業種の指数値をカンマ区切り>";q++;

業種の並び順は東証33業種の標準順で、`config/sectors.py` の SECTORS_S33 と一致することを
実データで照合済み（2026-08-11。ページのヘッダ業種名33個と1対1で突合）。
順序に依存する実装のため、値の個数が33でない応答は使わない。

⚠️ 取得失敗・日付不一致のときは空を返す（fail-soft）。従来動作を壊さない。
⚠️ 騰落率は連続する2営業日の指数値から自前で計算する。前日値が離れすぎている場合
   （履歴末尾に1年前の参照行が混ざる）は計算しない。
"""
import re
from datetime import date, datetime
from typing import Optional

import requests
from loguru import logger

from config.sectors import SECTORS_S33

SOURCE_URL = "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo_past.js"
SOURCE_LABEL = "nikkei225jp.com 業種別株価指数"

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://nikkei225jp.com/chart/gyoushu.php",
}
_TIMEOUT_SECONDS = 15

# 東証33業種の標準順。並び順そのものが業種の同定に使われるため固定する
_S33_ORDER: list[tuple[str, str]] = [
    (code, name) for code, name in SECTORS_S33.items() if code != "9999"
]

# 前日として採用してよい最大の間隔（暦日）。履歴末尾の1年前参照行を弾くための安全弁
_MAX_PREV_GAP_DAYS = 7


def _parse_history(text: str) -> dict[str, list[float]]:
    """JSテキストを {ISO日付: [33業種の指数値]} に変換する。"""
    history: dict[str, list[float]] = {}

    for row in re.findall(r'GY\[q\]="([^"]+)"', text):
        parts = row.split(",")
        if len(parts) < 3:
            continue

        try:
            row_date = datetime.strptime(parts[0].strip(), "%Y/%m/%d").date().isoformat()
        except ValueError:
            continue

        values: list[float] = []
        for raw in parts[2:]:
            try:
                values.append(float(raw))
            except ValueError:
                values = []
                break

        # 並び順で業種を同定しているため、本数が違う応答は使わない
        if len(values) != len(_S33_ORDER):
            logger.warning(
                f"業種別指数の本数が想定外のため無視: {row_date} / {len(values)}件"
                f"（想定 {len(_S33_ORDER)}件）"
            )
            continue

        history[row_date] = values

    return history


def _previous_trading_date(history: dict[str, list[float]], target: str) -> Optional[str]:
    """target の直前の営業日を返す。間隔が開きすぎていれば None。"""
    earlier = sorted(d for d in history if d < target)
    if not earlier:
        return None

    previous = earlier[-1]
    try:
        gap = (date.fromisoformat(target) - date.fromisoformat(previous)).days
    except ValueError:
        return None

    if gap > _MAX_PREV_GAP_DAYS:
        # 履歴末尾には1年前の参照行が混ざる。これを前日として扱わない
        return None
    return previous


def build_sector_returns(
    history: dict[str, list[float]],
    target_date: Optional[str] = None,
) -> list[dict]:
    """履歴から指定日の業種別騰落率を組み立てる。

    Returns:
        s33_code / sector_name / index_value / prev_value / change / change_pct / as_of
        を持つ dict のリスト。算出できなければ空リスト。
    """
    if not history:
        return []

    as_of = target_date or max(history)
    if as_of not in history:
        logger.info(f"業種別騰落率: {as_of} のデータがありません（取得元の履歴外）")
        return []

    previous = _previous_trading_date(history, as_of)
    if previous is None:
        logger.info(f"業種別騰落率: {as_of} の前営業日を特定できません")
        return []

    current_values = history[as_of]
    previous_values = history[previous]

    results: list[dict] = []
    for i, (code, name) in enumerate(_S33_ORDER):
        current = current_values[i]
        prev = previous_values[i]
        change = current - prev
        change_pct = (change / prev * 100) if prev else None
        results.append({
            "s33_code": code,
            "sector_name": name,
            "index_value": round(current, 2),
            "prev_value": round(prev, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "as_of": as_of,
            "prev_date": previous,
        })
    return results


def fetch_sector_returns(target_date: Optional[str] = None) -> list[dict]:
    """業種別の前日騰落率を取得する。失敗時は空リスト（fail-soft）。"""
    try:
        response = requests.get(SOURCE_URL, headers=_HTTP_HEADERS, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logger.warning(f"業種別株価指数の取得に失敗: {e}")
        return []

    if response.status_code != 200:
        logger.warning(f"業種別株価指数の取得に失敗: HTTP {response.status_code}")
        return []

    history = _parse_history(response.text)
    if not history:
        logger.warning("業種別株価指数を解析できませんでした（形式変更の可能性）")
        return []

    results = build_sector_returns(history, target_date)
    if results:
        logger.info(
            f"業種別騰落率を取得: {results[0]['as_of']}（前営業日 {results[0]['prev_date']}）"
            f" / {len(results)}業種"
        )
    return results


def returns_by_sector_code(target_date: Optional[str] = None) -> dict[str, dict]:
    """S33コードを鍵にした辞書で返す（プロンプト組み立て用）。"""
    return {r["s33_code"]: r for r in fetch_sector_returns(target_date)}


def format_quadrant(short_ratio_dod: Optional[float], change_pct: Optional[float]) -> str:
    """空売り比率の前日比と株価騰落率から4象限のラベルを返す。

    ⚠️ いずれも候補であり断定ではない。単日の組み合わせで方向を決めつけない。
    """
    if short_ratio_dod is None or change_pct is None:
        return ""
    if short_ratio_dod > 0 and change_pct > 0:
        return "比率上昇×株価上昇=売り吸収（踏み上げ・押し目買い優勢の可能性）"
    if short_ratio_dod > 0 and change_pct < 0:
        return "比率上昇×株価下落=方向性売り優勢の可能性"
    if short_ratio_dod < 0 and change_pct > 0:
        return "比率低下×株価上昇=ショートカバー主導の可能性"
    if short_ratio_dod < 0 and change_pct < 0:
        return "比率低下×株価下落=売り圧力後退でも買い不在の可能性"
    return ""
