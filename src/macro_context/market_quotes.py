"""
主要金融市場のライブ気配（先物・為替・指数・金利）を取得する。

このプロジェクトの方針どおり認証鍵は使わない。yfinance は公開 Yahoo Finance を
叩くだけでキー不要。Streamlit Cloud では稀にレート制限されるため、呼び出し側で
5分キャッシュ（@st.cache_data(ttl=300)）し、本モジュールは取得失敗を例外送出せず
fail-soft（ok=False）で返す。AIレポートにも `build_market_quotes_prompt_block()`
経由で「実測ブロック」として注入する。
"""
from __future__ import annotations

from dataclasses import dataclass


# (ラベル, Yahooティッカー, カテゴリ, 単位)
MARKET_INSTRUMENTS: list[tuple[str, str, str, str]] = [
    ("日経225先物(円建)", "NIY=F", "日本株", "pt"),
    ("日経平均", "^N225", "日本株", "pt"),
    ("TOPIX", "1348.T", "日本株", "pt"),  # 値はnikkei225jp.comの実指数で上書き（失敗時ETF1348）
    ("ナスダック100先物", "NQ=F", "米国株", "pt"),
    ("S&P500", "^GSPC", "米国株", "pt"),
    ("SOX半導体指数", "^SOX", "米国株", "pt"),
    ("ドル円", "USDJPY=X", "為替", "円"),
    ("ドル指数(DXY)", "DX-Y.NYB", "為替", ""),
    ("米10年金利", "^TNX", "金利・リスク", "%"),
    ("米30年金利", "^TYX", "金利・リスク", "%"),
    ("VIX恐怖指数", "^VIX", "金利・リスク", ""),
    ("WTI原油", "CL=F", "金利・リスク", "$"),
    ("金(Gold)", "GC=F", "金利・リスク", "$"),
]


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


def fetch_quotes(instruments=None) -> list[Quote]:
    """全銘柄のライブ気配を取得する。失敗は ok=False で返し、例外送出しない。"""
    instruments = instruments or MARKET_INSTRUMENTS

    try:
        import yfinance as yf
    except Exception as exc:  # ライブラリ未導入・import失敗
        return [
            Quote(label=l, ticker=t, category=c, unit=u, error=f"yfinance未利用: {exc}")
            for (l, t, c, u) in instruments
        ]

    tickers = [t for (_, t, _, _) in instruments]
    data = None
    try:
        data = yf.download(
            tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=True,
        )
    except Exception:
        data = None

    quotes: list[Quote] = []
    for (label, ticker, category, unit) in instruments:
        value = change = change_pct = None
        ok = False
        error = ""
        try:
            closes = _extract_closes(data, ticker)
            if closes:
                value = float(closes[-1])
                if len(closes) >= 2:
                    prev = float(closes[-2])
                    change = value - prev
                    change_pct = (change / prev * 100.0) if prev else None
                ok = True
            else:
                error = "no data"
        except Exception as exc:
            error = str(exc)
        quotes.append(
            Quote(
                label=label,
                ticker=ticker,
                category=category,
                unit=unit,
                value=value,
                change=change,
                change_pct=change_pct,
                ok=ok,
                error=error,
            )
        )

    # TOPIXは無料シンボルが無いため、nikkei225jp.comの実指数で上書き（精度確保）。
    site_df = _fetch_site_nikkei_topix()
    if site_df is not None and not site_df.empty:
        topix_last = float(site_df["topix"].iloc[-1])
        topix_prev = float(site_df["topix"].iloc[-2]) if len(site_df) >= 2 else None
        change = (topix_last - topix_prev) if topix_prev else None
        change_pct = (change / topix_prev * 100.0) if (change is not None and topix_prev) else None
        for i, q in enumerate(quotes):
            if q.ticker == "1348.T":
                quotes[i] = Quote(
                    label="TOPIX",
                    ticker=q.ticker,
                    category=q.category,
                    unit=q.unit,
                    value=topix_last,
                    change=change,
                    change_pct=change_pct,
                    ok=True,
                )
                break

    return quotes


def _extract_closes(data, ticker):
    """yf.download 結果から ticker の Close 系列（NaN除去）を取り出す。"""
    if data is None or len(data) == 0:
        return None

    import pandas as pd

    try:
        if isinstance(data.columns, pd.MultiIndex):
            if ticker not in set(data.columns.get_level_values(0)):
                return None
            series = data[ticker]["Close"].dropna()
        else:
            # 単一ティッカー時は階層なし
            series = data["Close"].dropna()
    except Exception:
        return None

    if series is None or series.empty:
        return None
    return series.tolist()


# NT倍率（日経平均 ÷ TOPIX）。TOPIX実指数の無料シンボルが無いため、実日経平均・実TOPIXを
# 日次JSONで公開している nikkei225jp.com を参照する（キー不要・2016年〜）。
SITE_BASE = "https://nikkei225jp.com"
SITE_NIKKEI_PATH = "/_data/_nfsWEB/HS_DATA_DAY/S111.json"  # 日経平均(実値)
SITE_TOPIX_PATH = "/_data/_nfsWEB/HS_DATA_DAY/S112.json"   # TOPIX(実指数)
_NT_PERIOD_DAYS = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 372, "2y": 744}


def _load_site_series(path: str):
    """nikkei225jp.com の `var Sxxx = [[ms, value], ...];` 形式JSONを読み込む。"""
    import json
    import re
    import urllib.request as request

    req = request.Request(
        SITE_BASE + path,
        headers={"User-Agent": "Mozilla/5.0", "Referer": SITE_BASE + "/data/nt.php"},
    )
    raw = request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    match = re.search(r"=\s*(\[.*\])\s*;?\s*$", raw, re.S)
    if not match:
        return None
    return json.loads(match.group(1))


def _fetch_site_nikkei_topix():
    """nikkei225jp.com から日経平均・TOPIXの実値時系列を取り、NT倍率を計算する。"""
    try:
        import pandas as pd

        nk = _load_site_series(SITE_NIKKEI_PATH)
        tp = _load_site_series(SITE_TOPIX_PATH)
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

    lines = ["【ライブ市場気配（実測・直近終値ベース）】:"]
    current_cat = None
    for q in observed:
        if q.category != current_cat:
            current_cat = q.category
            lines.append(f"  ＜{current_cat}＞")
        lines.append(f"    - {q.label}: {q.value_text}  前日比 {q.change_text}")
    nt = _current_nt_ratio(quotes)
    if nt is not None:
        lines.append(f"  ＜NT倍率＞")
        lines.append(f"    - NT倍率(日経平均÷TOPIX): {nt:.2f}（高=値がさ/グロース優位、低=内需/バリュー優位）")
    if missing:
        names = "、".join(q.label for q in missing)
        lines.append(f"  ※未取得（実測扱いせず断定しない）: {names}")
    lines.append("  注意: 上記は実測値として解釈してよい。これ以外の指標は実測扱いしない。")
    return "\n".join(lines)
