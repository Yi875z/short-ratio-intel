"""
J-Quants API (v2) クライアント。

⚠️ 同ディレクトリの `jquants_client.py` とは別物。あちらは名前に反して
   stock-marketdata.com のスクレイパーで、J-Quants API は一切使っていない。
   本物の J-Quants API を叩くのはこのファイルだけ。

認証は x-api-key ヘッダ方式（v2 から。有効期限なし）。v1 のメール+パスワードで
refreshToken から idToken を交換する手順は不要になった。

契約プランで叩けるエンドポイントが変わる。2026-08-29 に Light の実キーで実測:

    200 (Light で利用可)
        /equities/master              上場銘柄一覧（S33業種・市場区分 Mkt 付き）
        /equities/bars/daily          全銘柄日足（date 指定で1リクエスト・ページングなし）
        /indices/bars/daily/topix     TOPIX 四本値
        /markets/calendar             取引カレンダー
        /equities/investor-types      投資部門別（週次）

    403 (Standard 以上が必要)
        /markets/short-ratio          業種別空売り比率
        /indices/bars/daily           業種別など TOPIX 以外の指数四本値

したがって空売り比率そのものは J-Quants からは取得しない。従来どおり
JPX 公式PDF（jpx_pdf_client.py）が正であり、本クライアントの役割は
騰落銘柄数の材料（全銘柄日足＋銘柄一覧）・TOPIX・営業日カレンダーの取得に限る。
"""
from __future__ import annotations

import time
from datetime import datetime

import requests
from loguru import logger

from config.settings import (
    JQUANTS_API_BASE_URL,
    JQUANTS_API_KEY,
    JQUANTS_MAX_RETRIES,
    JQUANTS_MIN_REQUEST_INTERVAL_SEC,
    JQUANTS_REQUEST_TIMEOUT_SEC,
)

# 取引所の市場区分コード（/equities/master の Mkt）。
# 空売り集計（JPX公式PDF・東証全体）とは対象範囲が異なるため、
# 騰落銘柄数は必ずこのスコープ付きで扱い、空売り代金と混ぜて除算しない。
MARKET_CODE_PRIME = "0111"
MARKET_CODE_STANDARD = "0112"
MARKET_CODE_GROWTH = "0113"
MARKET_CODE_OTHER = "0109"      # ETF・REIT・優先株など
MARKET_CODE_PRO = "0105"        # TOKYO PRO MARKET（日足が付かないため騰落計算の対象外）


class JQuantsError(RuntimeError):
    """J-Quants API 呼び出しの基底エラー。"""


class JQuantsNotConfiguredError(JQuantsError):
    """APIキーが未設定。呼び出し側は機能を無効化して処理を続けること。"""


class JQuantsNotEntitledError(JQuantsError):
    """契約プランでは使えないエンドポイント（HTTP 403）。"""


class JQuantsRequestError(JQuantsError):
    """通信失敗・想定外のHTTPステータス。"""


class JQuantsApiClient:
    """J-Quants API v2 の薄いクライアント。

    - ページング（pagination_key）は内部で回収して1つのリストにまとめる。
    - 429 / 5xx は指数バックオフで再試行する。403 / 400 は再試行しない。
    - レート制限（Light は 60 req/分）に触れないよう最短間隔を空ける。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = JQUANTS_API_BASE_URL,
        timeout: int = JQUANTS_REQUEST_TIMEOUT_SEC,
        min_interval_sec: float = JQUANTS_MIN_REQUEST_INTERVAL_SEC,
        max_retries: int = JQUANTS_MAX_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else JQUANTS_API_KEY).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval_sec = min_interval_sec
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._last_request_at = 0.0

    @property
    def is_configured(self) -> bool:
        """APIキーが設定されているか。未設定なら呼び出し側は機能を落として続行する。"""
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # エンドポイント
    # ------------------------------------------------------------------
    def get_listed_master(self, target_date: str | None = None) -> list[dict]:
        """上場銘柄一覧を返す。

        target_date を渡すとその日時点の母集団になる。騰落銘柄数を数えるときは
        必ず対象日を渡すこと（省略すると翌営業日時点の一覧が返り、新規上場・
        市場変更のぶんだけ母集団がずれる）。
        """
        params: dict[str, str] = {}
        if target_date:
            params["date"] = _normalize_date(target_date)
        return self._get("/equities/master", params)

    def get_daily_bars(self, target_date: str) -> list[dict]:
        """指定日の全銘柄日足を返す（O/H/L/C・Vo・Va・AdjC・MktCap 等）。

        値は生値（C）と調整後（AdjC）の両方が入る。騰落判定は分割・併合を
        またいでも壊れないよう AdjC 同士で比較すること。
        """
        return self._get("/equities/bars/daily", {"date": _normalize_date(target_date)})

    def get_topix_bars(self, from_date: str, to_date: str) -> list[dict]:
        """TOPIX の日次四本値を返す（Date/O/H/L/C）。"""
        return self._get(
            "/indices/bars/daily/topix",
            {"from": _normalize_date(from_date), "to": _normalize_date(to_date)},
        )

    def get_trading_calendar(self, from_date: str, to_date: str) -> list[dict]:
        """取引カレンダーを返す（Date と HolDiv。"1"=営業日 / "0"=休業日）。"""
        return self._get(
            "/markets/calendar",
            {"from": _normalize_date(from_date), "to": _normalize_date(to_date)},
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> list[dict]:
        if not self.is_configured:
            raise JQuantsNotConfiguredError(
                "JQUANTS_API_KEY が未設定です。J-Quants 由来の指標は無効化されます。"
            )

        records: list[dict] = []
        query = dict(params)
        seen_keys: set[str] = set()

        while True:
            payload = self._request(path, query)
            records.extend(payload.get("data") or [])

            next_key = payload.get("pagination_key")
            if not next_key:
                break
            # 同じキーが返り続ける異常応答で無限ループしないよう打ち切る。
            if next_key in seen_keys:
                logger.warning(f"J-Quants: pagination_key が循環したため打ち切ります ({path})")
                break
            seen_keys.add(next_key)
            query = dict(params, pagination_key=next_key)

        return records

    def _request(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"x-api-key": self.api_key}
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(f"J-Quants 通信失敗 ({path}) {attempt}/{self.max_retries}: {exc}")
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise JQuantsRequestError(
                        f"J-Quants の応答がJSONとして読めません ({path}): {exc}"
                    ) from exc

            if resp.status_code == 403:
                # 契約プランの範囲外。再試行しても回復しないので即座に打ち切る。
                raise JQuantsNotEntitledError(
                    f"契約プランでは利用できないエンドポイントです ({path}): "
                    f"{_short_body(resp.text)}"
                )

            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = JQuantsRequestError(
                    f"HTTP {resp.status_code} ({path}): {_short_body(resp.text)}"
                )
                logger.warning(
                    f"J-Quants HTTP {resp.status_code} ({path}) "
                    f"{attempt}/{self.max_retries} — 再試行します"
                )
                self._sleep_backoff(attempt)
                continue

            raise JQuantsRequestError(
                f"J-Quants が想定外のステータスを返しました "
                f"HTTP {resp.status_code} ({path}): {_short_body(resp.text)}"
            )

        raise JQuantsRequestError(
            f"J-Quants の呼び出しに {self.max_retries} 回失敗しました ({path}): {last_error}"
        )

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        wait = self.min_interval_sec - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(min(2 ** (attempt - 1), 8))


def _normalize_date(value: str) -> str:
    """YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD を YYYY-MM-DD に揃える。"""
    raw = value.strip().replace("/", "-")
    if "-" in raw:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    else:
        parsed = datetime.strptime(raw, "%Y%m%d")
    return parsed.strftime("%Y-%m-%d")


def _short_body(text: str, limit: int = 200) -> str:
    body = " ".join((text or "").split())
    return body[:limit]
