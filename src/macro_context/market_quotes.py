"""
主要金融市場のライブ気配（先物・為替・指数・金利・商品）を取得する。

データ源は nikkei225jp.com が公開する日次JSON時系列（`var Sxxx = [[ms, value], ...]`）。
認証キー不要で、Streamlit Cloud からも安定取得できる（yfinanceはCloudで
レート制限/ブロックされ「取得失敗」になったため撤去）。各シリーズコードは実値の
クロス照合で同定済み（S&P500/SOX/VIX/米金利/WTI/金 等は外部値と一致を確認）。

参照ページ: /cme/(S233) /chart/ /nasdaq/(S214) /oil/(S921,S931) /fx/(S511) /bond/(S811,S812)
AIレポートには `build_market_quotes_prompt_block()` 経由で「実測ブロック」として注入する。
"""
from __future__ import annotations

import datetime as _dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


SITE_BASE = "https://nikkei225jp.com"
_SERIES_PATH = "/_data/_nfsWEB/HS_DATA_DAY/S{code}.json"
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": SITE_BASE + "/"}

# (ラベル, シリーズコード, カテゴリ, 単位)。コードは nikkei225jp.com の日次JSON。
MARKET_INSTRUMENTS: list[tuple[str, int, str, str]] = [
    ("日経225先物(CME)", 233, "日本株", "pt"),
    ("日経平均(現物)", 111, "日本株", "pt"),
    ("TOPIX", 112, "日本株", "pt"),
    ("ナスダック100", 214, "米国株", "pt"),
    ("S&P500", 213, "米国株", "pt"),
    ("NYダウ", 211, "米国株", "pt"),
    ("SOX半導体指数", 611, "米国株", "pt"),
    ("ドル円", 511, "為替", "円"),
    ("米10年金利", 811, "金利・リスク", "%"),
    ("米30年金利", 812, "金利・リスク", "%"),
    ("VIX恐怖指数", 621, "金利・リスク", ""),
    ("WTI原油", 921, "金利・リスク", "$"),
    ("金(Gold)", 931, "金利・リスク", "$"),
]

# NT倍率（日経平均 ÷ TOPIX）用のシリーズ。
_NT_NIKKEI_CODE = 111
_NT_TOPIX_CODE = 112
_NT_PERIOD_DAYS = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 372, "2y": 744}


@dataclass(frozen=True)
class Quote:
    """1銘柄分のライブ気配（直近終値ベース）。取得失敗時は ok=False。"""

    label: str
    ticker: str
    category: str
    unit: str
    value: float | None = None
    change: float | None = None
    change_pct: float | None = None
    ok: bool = False
    error: str = ""
    as_of: str = ""

    @property
    def value_text(self) -> str:
        if not self.ok or self.value is None:
            return "取得失敗"
        return f"{self.value:,.2f}{self.unit}"

    @property
    def change_text(self) -> str:
        if not self.ok or self.change is None or self.change_pct is None:
            return "—"
        sign = "+" if self.change >= 0 else ""
        return f"{sign}{self.change:,.2f} ({sign}{self.change_pct:.2f}%)"


def _load_series(code: int):
    """nikkei225jp.com の `var Sxxx = [[ms, value], ...];` JSONを配列で返す。失敗時 None。"""
    import json
    import re
    import urllib.request as request

    req = request.Request(SITE_BASE + _SERIES_PATH.format(code=code), headers=_HTTP_HEADERS)
    raw = request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    match = re.search(r"=\s*(\[.*\])\s*;?\s*$", raw, re.S)
    if not match:
        return None
    return json.loads(match.group(1))


def _quote_from_instrument(item) -> Quote:
    label, code, category, unit = item
    try:
        arr = _load_series(code)
        if not arr:
            return Quote(label, str(code), category, unit, error="no data")
        last = arr[-1]
        value = float(last[1])
        as_of = _dt.datetime.utcfromtimestamp(last[0] / 1000).date().isoformat()
        change = change_pct = None
        if len(arr) >= 2 and arr[-2][1]:
            prev = float(arr[-2][1])
            change = value - prev
            change_pct = change / prev * 100.0
        return Quote(
            label=label,
            ticker=str(code),
            category=category,
            unit=unit,
            value=value,
            change=change,
            change_pct=change_pct,
            ok=True,
            as_of=as_of,
        )
    except Exception as exc:
        return Quote(label, str(code), category, unit, error=str(exc))


def fetch_quotes(instruments=None) -> list[Quote]:
    """全銘柄のライブ気配を取得する。失敗は ok=False で返し、例外送出しない。"""
    instruments = instruments or MARKET_INSTRUMENTS
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            return list(executor.map(_quote_from_instrument, instruments))
    except Exception:
        # スレッド生成に失敗した場合は逐次でフォールバック
        return [_quote_from_instrument(item) for item in instruments]


def _fetch_site_nikkei_topix():
    """日経平均・TOPIXの実値時系列を取り、NT倍率を計算した DataFrame を返す。失敗時 None。"""
    try:
        import pandas as pd

        nk = _load_series(_NT_NIKKEI_CODE)
        tp = _load_series(_NT_TOPIX_CODE)
        if not nk or not tp:
            return None
        nk_map = {row[0]: row[1] for row in nk}
        tp_map = {row[0]: row[1] for row in tp}
        rows = []
        for ts in sorted(set(nk_map) & set(tp_map)):
            topix = tp_map[ts]
            if not topix:
                continue
            rows.append(
                {
                    "date": pd.to_datetime(ts, unit="ms"),
                    "nikkei": nk_map[ts],
                    "topix": topix,
                    "nt_ratio": nk_map[ts] / topix,
                }
            )
        df = pd.DataFrame(rows)
        return df if not df.empty else None
    except Exception:
        return None


def fetch_nt_ratio_history(period: str = "6mo"):
    """NT倍率（日経平均÷TOPIX）の推移を DataFrame で返す。失敗時は None。"""
    df = _fetch_site_nikkei_topix()
    if df is None or df.empty:
        return None

    import pandas as pd

    days = _NT_PERIOD_DAYS.get(period, 186)
    cutoff = df["date"].max() - pd.Timedelta(days=days)
    out = df[df["date"] >= cutoff].copy()
    return out if not out.empty else df


def _current_nt_ratio(quotes=None) -> float | None:
    """現在のNT倍率を返す（nikkei225jp.com の実値ベース）。"""
    df = _fetch_site_nikkei_topix()
    if df is not None and not df.empty:
        return float(df["nt_ratio"].iloc[-1])
    return None


def build_market_quotes_prompt_block(quotes=None) -> str:
    """AIレポート用：ライブ市場気配を「実測ブロック」として整形する。"""
    if quotes is None:
        quotes = fetch_quotes()

    observed = [q for q in quotes if q.ok]
    missing = [q for q in quotes if not q.ok]

    if not observed:
        return (
            "【ライブ市場気配（実測）】:\n"
            "  取得失敗。今回は実測の先物・為替・金利・指数の値なし。"
            "数値・方向性を事実として断定しない。"
        )

    as_of = next((q.as_of for q in observed if q.as_of), "")
    header = "【ライブ市場気配（実測・直近終値ベース"
    header += f"・{as_of}時点" if as_of else ""
    header += "）】:"
    lines = [header]

    current_cat = None
    for q in observed:
        if q.category != current_cat:
            current_cat = q.category
            lines.append(f"  ＜{current_cat}＞")
        lines.append(f"    - {q.label}: {q.value_text}  前日比 {q.change_text}")

    nt = _current_nt_ratio(quotes)
    if nt is not None:
        lines.append("  ＜NT倍率＞")
        lines.append(
            f"    - NT倍率(日経平均÷TOPIX): {nt:.2f}（高=値がさ/グロース優位、低=内需/バリュー優位）"
        )
    if missing:
        names = "、".join(q.label for q in missing)
        lines.append(f"  ※未取得（実測扱いせず断定しない）: {names}")
    lines.append("  注意: 上記は実測値として解釈してよい。これ以外の指標は実測扱いしない。")
    return "\n".join(lines)
