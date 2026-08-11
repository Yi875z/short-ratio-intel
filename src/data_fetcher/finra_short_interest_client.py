"""
FINRA 空売り残高（Consolidated Short Interest）クライアント

米国の空売り「残高」を FINRA 公式APIから取得する。認証・APIキー不要。

⚠️ 日次ショートボリューム（フロー）とは別概念である（QCルール2）。
   フローは「その日に空売りとして約定・報告された株数」で、当日中に買い戻された分や
   マーケットメイクの一時的ショートを含む。
   残高は「基準日時点で未決済のまま残っている空売り」で、原則として月2回しか更新されない。
   両者は一致しないし、日次データから残高を推定してはならない。

⚠️ 残高は公表までに時間差がある。基準日から2週間前後遅れて公表されるため、
   利用側には必ず「基準日」と「そこからの経過日数」を示すこと。
"""
import csv
import io
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from loguru import logger

_API_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"

SOURCE_NAME = "FINRA_SHORT_INTEREST"

_PAGE_SIZE = 5000          # サーバ側の1回あたり上限
_MAX_PAGES = 10            # 暴走防止（1決済日あたり実測22,375件＝5ページ）
_REQUEST_TIMEOUT_SECONDS = 90
_MAX_NETWORK_RETRIES = 3
_RETRY_BASE_SECONDS = 1.0
_LOOKBACK_DAYS_FOR_LATEST = 75   # 最新の基準日を探すさかのぼり幅（隔週なので余裕を持つ）

_HEADERS = {"Content-Type": "application/json"}


def _to_int(value) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


def _to_float(value) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def build_record(row: dict) -> dict:
    """APIの1行を正規化レコードにする。欠落は None を明示的に入れる。"""
    return {
        "SettlementDate": (row.get("settlementDate") or "").strip() or None,
        "Ticker": (row.get("symbolCode") or "").strip() or None,
        "IssueName": (row.get("issueName") or "").strip() or None,
        "CurrentShortPosition": _to_int(row.get("currentShortPositionQuantity")),
        "PreviousShortPosition": _to_int(row.get("previousShortPositionQuantity")),
        "AverageDailyVolume": _to_int(row.get("averageDailyVolumeQuantity")),
        "DaysToCover": _to_float(row.get("daysToCoverQuantity")),
        "ChangePercent": _to_float(row.get("changePercent")),
        "Source": SOURCE_NAME,
    }


def days_since_settlement(settlement_date: str, as_of: Optional[str] = None) -> Optional[int]:
    """基準日からの経過日数（暦日）を返す。

    残高は基準日時点のスナップショットで、公表も利用も後になる。
    「いつ時点の数字なのか」を必ず添えるためのヘルパー。
    """
    if not settlement_date:
        return None
    try:
        settled = date.fromisoformat(settlement_date)
        reference = date.fromisoformat(as_of) if as_of else date.today()
    except (TypeError, ValueError):
        return None
    return (reference - settled).days


class FinraShortInterestClient:
    """FINRA 空売り残高の取得クライアント"""

    def __init__(self, request_interval: float = 0.3) -> None:
        self.request_interval = request_interval
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------

    def get_latest_settlement_date(self, probe_ticker: str = "NVDA") -> Optional[str]:
        """公開済みで最も新しい基準日を返す。

        基準日はデータの分割キーなので、まず1銘柄で直近を引いて特定する
        （全銘柄を舐めるより圧倒的に軽い）。
        """
        since = (date.today() - timedelta(days=_LOOKBACK_DAYS_FOR_LATEST)).isoformat()
        rows = self._query([
            {"fieldName": "symbolCode", "fieldValue": probe_ticker, "compareType": "EQUAL"},
            {"fieldName": "settlementDate", "fieldValue": since, "compareType": "GREATER"},
        ], limit=50)

        dates = [r.get("settlementDate") for r in rows if r.get("settlementDate")]
        if not dates:
            logger.warning(f"空売り残高の基準日を特定できませんでした（{probe_ticker}・{since}以降）")
            return None
        return max(dates)

    def get_short_interest(
        self,
        settlement_date: str,
        tickers: Optional[list[str]] = None,
    ) -> list[dict]:
        """指定基準日の空売り残高を返す。tickers 指定で絞り込む。"""
        wanted = set(tickers) if tickers else None
        records: list[dict] = []
        offset = 0

        for _ in range(_MAX_PAGES):
            page = self._query(
                [{"fieldName": "settlementDate", "fieldValue": settlement_date, "compareType": "EQUAL"}],
                limit=_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break

            for row in page:
                ticker = (row.get("symbolCode") or "").strip()
                if wanted is not None and ticker not in wanted:
                    continue
                records.append(build_record(row))

            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

            # 対象銘柄がすべて揃ったら残りのページは引かない
            if wanted is not None and {r["Ticker"] for r in records} >= wanted:
                break

        logger.info(f"空売り残高 取得: 基準日 {settlement_date} / {len(records)}銘柄")
        return records

    def get_latest_short_interest(
        self,
        tickers: Optional[list[str]] = None,
    ) -> tuple[Optional[str], list[dict]]:
        """最新の基準日とその残高を返す。特定できなければ (None, [])。"""
        settlement_date = self.get_latest_settlement_date()
        if not settlement_date:
            return None, []
        return settlement_date, self.get_short_interest(settlement_date, tickers=tickers)

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _query(self, compare_filters: list[dict], limit: int, offset: int = 0) -> list[dict]:
        """APIへPOSTしてCSVを辞書のリストで返す。失敗時は空リスト（fail-soft）。"""
        payload = json.dumps({
            "limit": limit,
            "offset": offset,
            "compareFilters": compare_filters,
        }).encode("utf-8")

        for attempt in range(1, _MAX_NETWORK_RETRIES + 1):
            self._throttle()
            try:
                response = requests.post(
                    _API_URL, data=payload, headers=_HEADERS,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                if attempt >= _MAX_NETWORK_RETRIES:
                    logger.warning(f"空売り残高の取得に失敗（通信エラー）: {e}")
                    return []
                time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if response.status_code == 200:
                try:
                    return list(csv.DictReader(io.StringIO(response.text)))
                except csv.Error as e:
                    logger.warning(f"空売り残高のCSV解析に失敗: {e}")
                    return []

            if response.status_code == 400:
                # 問い合わせ条件の誤り。リトライしても直らないので即返す
                logger.warning(f"空売り残高の問い合わせが不正: {response.text[:200]}")
                return []

            if attempt >= _MAX_NETWORK_RETRIES:
                logger.warning(f"空売り残高の取得に失敗: HTTP {response.status_code}")
                return []
            time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

        return []

    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if 0 < elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()
