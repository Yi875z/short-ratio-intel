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

from loguru import logger


SITE_BASE = "https://nikkei225jp.com"
_SERIES_PATH = "/_data/_nfsWEB/HS_DATA_DAY/S{code}.json"
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": SITE_BASE + "/"}

# (ラベル, シリーズコード, カテゴリ, 単位)。コードは nikkei225jp.com の日次JSON。
# 日経225先物は日次確定足だとザラ場中1営業日遅れるため、準リアルタイムのティック
# (_TICK_INSTRUMENTS)で別途取得する。ここには含めない。
MARKET_INSTRUMENTS: list[tuple[str, int, str, str]] = [
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

# 準リアルタイム（約10分遅れ）ティック。[code, ms, value] のログで各コードの最終値が現値。
# 日経先物2系統(136/191)とドル円(511)を near-real-time 化する。
_TICK_PATH = "/_data/_nfsWEB/hs_data/hs_tick2.json"
# (tickコード, ラベル, カテゴリ, 単位, 前日終値の日次シリーズコード)
_TICK_INSTRUMENTS = [
    (136, "日経225先物(大取)", "日本株", "pt", 233),
    (191, "日経225先物(CME円建)", "日本株", "pt", 233),
    (511, "ドル円", "為替", "円", 511),
]


# 日経VI（日経平均ボラティリティ指数）。nikkei225jp.com は日経VIの日次系列を
# 持たない（600番台コードを実値照合したが一致なし）ため、CLAUDE.md 第一候補の
# stock-marketdata.com から取得する。同ページの日次テーブルと公表値を複数日照合済み。
_NIKKEI_VI_URL = "https://stock-marketdata.com/vi.html"


def _jst_date(ms: float):
    """unix ms を JST の日付に変換（サイトの足は15:00 UTC=翌00:00 JSTスタンプ）。"""
    return (_dt.datetime.utcfromtimestamp(ms / 1000) + _dt.timedelta(hours=9)).date()


def _jst_datetime(ms: float):
    return _dt.datetime.utcfromtimestamp(ms / 1000) + _dt.timedelta(hours=9)


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
        as_of = _jst_date(last[0]).isoformat()
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


def _load_tick_latest() -> dict:
    """hs_tick2.json を読み、{code: (value, ms)} で各コードの最新値を返す。失敗時 {}。"""
    import json
    import re
    import urllib.request as request

    try:
        req = request.Request(SITE_BASE + _TICK_PATH, headers=_HTTP_HEADERS)
        raw = request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        rows = json.loads(re.search(r"=\s*(\[.*\])\s*;?\s*$", raw, re.S).group(1))
        latest = {}
        for r in rows:
            if len(r) >= 3 and isinstance(r[0], int):
                latest[r[0]] = (r[2], r[1])
        return latest
    except Exception:
        return {}


def _fetch_realtime_quotes():
    """準リアルタイム（約10分遅れ）の日経先物・ドル円を hs_tick2.json から取得する。

    戻り値: (futures_quotes, fx_override) — 先物は新規カード、ドル円は日次値の上書き用。
    """
    latest = _load_tick_latest()
    if not latest:
        return [], None

    # 前日終値（日次シリーズの最終確定値）を必要なコードだけ取得
    prev_close = {}
    for code, _label, _cat, _unit, prev_code in _TICK_INSTRUMENTS:
        if code in latest and prev_code not in prev_close:
            try:
                arr = _load_series(prev_code)
                prev_close[prev_code] = float(arr[-1][1]) if arr else None
            except Exception:
                prev_close[prev_code] = None

    futures, fx_override = [], None
    for code, label, category, unit, prev_code in _TICK_INSTRUMENTS:
        if code not in latest:
            continue
        value, ms = float(latest[code][0]), latest[code][1]
        prev = prev_close.get(prev_code)
        change = (value - prev) if prev else None
        change_pct = (change / prev * 100.0) if (change is not None and prev) else None
        q = Quote(
            label=label,
            ticker=str(code),
            category=category,
            unit=unit,
            value=value,
            change=change,
            change_pct=change_pct,
            ok=True,
            as_of=_jst_datetime(ms).strftime("%m-%d %H:%M"),
        )
        if code == 511:
            fx_override = q
        else:
            futures.append(q)
    return futures, fx_override


def _parse_smd_number(text) -> float | None:
    """stock-marketdata のセル文字列を float に変換。空・非数値は None。"""
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch_nikkei_vi_quote() -> Quote:
    """日経VIを stock-marketdata.com の日次テーブルから取得する。失敗時 ok=False。

    テーブルは最新行が data-y="2"、列 data-x=0:日付 / 1:終値 / 2:前日比 / 3:前日比%。
    日本市場のオプション性リスク（恐怖度）指標で、米VIXと対で解釈できる。
    """
    import re
    import urllib.request as request

    label, category, unit = "日経VI", "金利・リスク", ""
    try:
        req = request.Request(_NIKKEI_VI_URL, headers={"User-Agent": "Mozilla/5.0"})
        html = request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        cells = {
            int(m.group(1)): m.group(2).strip()
            for m in re.finditer(
                r'data-x="(\d+)"\s+data-y="2"[^>]*>([^<]*)</td>', html
            )
        }
        value = _parse_smd_number(cells.get(1))
        if value is None:
            return Quote(label, "NKVI", category, unit, error="no data")
        as_of = cells.get(0, "").replace("/", "-").strip()
        return Quote(
            label=label,
            ticker="NKVI",
            category=category,
            unit=unit,
            value=value,
            change=_parse_smd_number(cells.get(2)),
            change_pct=_parse_smd_number(cells.get(3)),
            ok=True,
            as_of=as_of,
        )
    except Exception as exc:
        return Quote(label, "NKVI", category, unit, error=str(exc))


def _insert_nikkei_vi(daily: list[Quote], vi: Quote) -> list[Quote]:
    """日経VIをVIX（ticker=621）の直後へ差し込み、金利・リスク群にまとめる。

    VIXが無い場合は末尾に追加する（レンダリングのカテゴリ連続性を保つ）。
    """
    out: list[Quote] = []
    inserted = False
    for q in daily:
        out.append(q)
        if q.ticker == "621":
            out.append(vi)
            inserted = True
    if not inserted:
        out.append(vi)
    return out


def fetch_quotes(instruments=None) -> list[Quote]:
    """全銘柄のライブ気配を取得する。失敗は ok=False で返し、例外送出しない。

    日経先物2系統(大取/CME)とドル円は準リアルタイム(約10分遅れ)、それ以外は日次終値。
    日経VIのみ別ソース(stock-marketdata.com)から取得しVIXの隣へ配置する。
    """
    instruments = instruments or MARKET_INSTRUMENTS
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            daily = list(executor.map(_quote_from_instrument, instruments))
    except Exception:
        daily = [_quote_from_instrument(item) for item in instruments]

    futures, fx_override = _fetch_realtime_quotes()
    if fx_override is not None:
        daily = [fx_override if q.ticker == "511" else q for q in daily]

    daily = _insert_nikkei_vi(daily, _fetch_nikkei_vi_quote())
    # 先物カードは日本株の先頭に置く
    return futures + daily


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
                    # サイトの足は 15:00 UTC（＝翌 00:00 JST）でスタンプされているため、
                    # 生の ms をそのまま解釈すると取引日が1日前にずれる。
                    # （8/21終値 66,016.36 が 8/20 15:00 として入っていた）
                    "date": pd.Timestamp(_jst_date(ts)),
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


# 日経平均の日足OHLC。Yahoo チャートAPI の `^N225` は open/high/low/close が揃う。
# ⚠️ TOPIX の OHLC は無料・認証不要の範囲に存在しない（2026-08-25 調査）。
#    Yahoo は ^TPX / ^TOPX / 998405.T すべて空、stooq は JS 検証ページ化して死亡、
#    stock-marketdata.com/nikkei225.html は終値のみで始値・高値・安値の列が無い。
#    したがってロウソク足は日経平均だけ。TOPIX は終値ラインで表示する。
_NIKKEI_YAHOO_SYMBOL = "^N225"


def fetch_nikkei_topix_close_history(from_date=None, to_date=None):
    """日経平均・TOPIXの終値時系列を DataFrame(date, nikkei, topix) で返す。失敗時 None。

    出所は nikkei225jp.com の日次JSON。Yahoo では TOPIX を取得できないため、
    実TOPIXの時系列が取れる唯一の実用ソースになっている。
    """
    df = _fetch_site_nikkei_topix()
    if df is None or df.empty:
        return None

    import pandas as pd

    out = df[["date", "nikkei", "topix"]].copy()
    if from_date is not None:
        out = out[out["date"] >= pd.to_datetime(from_date)]
    if to_date is not None:
        out = out[out["date"] <= pd.to_datetime(to_date)]
    return out if not out.empty else None


def fetch_nikkei_ohlc_history(from_date, to_date):
    """日経平均の日足OHLCを DataFrame(date, open, high, low, close) で返す。失敗時 None。

    Yahoo チャートAPI を叩く既存の UsPriceClient をそのまま使う（HTTPコードを重複させない）。
    to_yahoo_symbol() は "/"→"-" の変換だけなので ^N225 は素通りし、parse_chart_payload() は
    meta.gmtoffset で取引所ローカル日付へ換算するため JST の営業日で正しく並ぶ。
    """
    try:
        import pandas as pd

        from src.data_fetcher.us_price_client import UsPriceClient

        records = UsPriceClient().get_daily_ohlcv(_NIKKEI_YAHOO_SYMBOL, from_date, to_date)
        if not records:
            return None

        out = pd.DataFrame([
            {
                "date": pd.to_datetime(r["Date"]),
                "open": r["Open"],
                "high": r["High"],
                "low": r["Low"],
                "close": r["Close"],
            }
            for r in records
        ])
        # 休場・未確定の足は None で来る。ロウソク足は4値揃っていないと描けない。
        out = out.dropna(subset=["open", "high", "low", "close"])
        return out if not out.empty else None
    except Exception as exc:
        logger.warning(f"日経平均の日足OHLC取得に失敗: {exc}")
        return None


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
