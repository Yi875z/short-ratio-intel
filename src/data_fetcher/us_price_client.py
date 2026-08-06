"""
米国株の日足OHLCV取得クライアント（Yahoo Finance chart API）

ログイン・APIキー不要のJSONエンドポイントを直接叩く。
yfinance ライブラリは使わない（Streamlit Cloud での取得失敗歴があり、依存も増やさないため）。

⚠️ ここで取得する出来高は consolidated volume（市場全体）である。
   FINRA のショートボリューム比率の分母に使ってはならない（QCルール1）。
   用途は騰落率・終値位置(CLV)・出来高比といった文脈把握に限る。
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from loguru import logger

from config.us_universe import to_yahoo_symbol
from src.data_fetcher.finra_client import normalize_date

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_MAX_NETWORK_RETRIES = 3
_RETRY_BASE_SECONDS = 1.0
_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_REQUEST_INTERVAL = 0.4


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def build_price_record(
    date_iso: str,
    ticker: str,
    open_: Optional[float] = None,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    adj_close: Optional[float] = None,
    market_volume: Optional[float] = None,
) -> dict:
    """欠落フィールドを None で明示的に埋めた価格レコードを返す。"""
    return {
        "Date": date_iso,
        "Ticker": ticker,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "AdjClose": adj_close,
        "MarketVolume": market_volume,   # ⚠️ 比率の分母に使わない
    }


def parse_chart_payload(payload: dict, ticker: str) -> list[dict]:
    """Yahoo chart API のJSONを日足レコードのリストへ変換する。

    値が欠けている足（休場・データ未確定）は None のまま行を作る。
    日付は取引所ローカル時刻へ換算して決定する。
    """
    if not payload:
        return []

    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        error = chart.get("error")
        if error:
            logger.warning(f"Yahoo chart API エラー: {ticker} / {error}")
        return []

    result = results[0]
    timestamps = result.get("timestamp") or []
    if not timestamps:
        return []

    meta = result.get("meta") or {}
    gmt_offset = int(meta.get("gmtoffset") or 0)

    indicators = result.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0] or {}
    adj_series = (indicators.get("adjclose") or [{}])[0] or {}

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []
    adj_closes = adj_series.get("adjclose") or []

    def _at(series: list, index: int):
        return series[index] if index < len(series) else None

    records: list[dict] = []
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        # 取引所ローカル時刻の日付を取引日とする
        local_dt = datetime.fromtimestamp(int(ts) + gmt_offset, tz=timezone.utc)
        records.append(build_price_record(
            date_iso=local_dt.date().isoformat(),
            ticker=ticker,
            open_=_to_float(_at(opens, i)),
            high=_to_float(_at(highs, i)),
            low=_to_float(_at(lows, i)),
            close=_to_float(_at(closes, i)),
            adj_close=_to_float(_at(adj_closes, i)),
            market_volume=_to_float(_at(volumes, i)),
        ))
    return records


class UsPriceClient:
    """Yahoo Finance chart API の日足取得クライアント"""

    def __init__(self, request_interval: float = _DEFAULT_REQUEST_INTERVAL) -> None:
        self.request_interval = request_interval
        self._last_request_at: float = 0.0

    def get_daily_ohlcv(self, ticker: str, from_date, to_date) -> list[dict]:
        """指定ティッカーの期間日足を返す。取得失敗時は空リスト（fail-soft）。"""
        start = datetime.strptime(normalize_date(from_date), "%Y-%m-%d").date()
        end = datetime.strptime(normalize_date(to_date), "%Y-%m-%d").date()

        # 端の足が欠けないよう前後に余裕を持たせる
        period1 = int(datetime.combine(
            start - timedelta(days=4), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp())
        period2 = int(datetime.combine(
            end + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp())

        payload = self._request(ticker, period1, period2)
        if payload is None:
            return []

        records = parse_chart_payload(payload, ticker)
        # 要求範囲外の足を落とす
        records = [
            r for r in records
            if start.isoformat() <= r["Date"] <= end.isoformat()
        ]
        logger.debug(f"日足取得: {ticker} / {len(records)}件")
        return records

    def get_daily_ohlcv_bulk(
        self,
        tickers: list[str],
        from_date,
        to_date,
    ) -> list[dict]:
        """複数ティッカーの日足をまとめて取得する。1銘柄の失敗で全体を止めない。"""
        records: list[dict] = []
        failed: list[str] = []
        for ticker in tickers:
            rows = self.get_daily_ohlcv(ticker, from_date, to_date)
            if rows:
                records.extend(rows)
            else:
                failed.append(ticker)

        if failed:
            logger.warning(f"日足を取得できなかった銘柄: {', '.join(failed)}")
        logger.info(f"日足取得完了: {len(tickers) - len(failed)}/{len(tickers)}銘柄 / {len(records)}レコード")
        return records

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _request(self, ticker: str, period1: int, period2: int) -> Optional[dict]:
        url = _CHART_URL.format(symbol=to_yahoo_symbol(ticker))
        params = {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
        }

        for attempt in range(1, _MAX_NETWORK_RETRIES + 1):
            self._throttle()
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=_HEADERS,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                if attempt >= _MAX_NETWORK_RETRIES:
                    logger.warning(f"日足取得失敗（通信エラー）: {ticker} / {e}")
                    return None
                time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as e:
                    logger.warning(f"日足のJSON解析に失敗: {ticker} / {e}")
                    return None

            if response.status_code == 404:
                logger.warning(f"銘柄が見つかりません: {ticker}")
                return None

            if attempt >= _MAX_NETWORK_RETRIES:
                logger.warning(f"日足取得失敗: {ticker} / HTTP {response.status_code}")
                return None
            time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

        return None

    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if 0 < elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()
