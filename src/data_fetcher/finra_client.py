"""
FINRA Daily Short Sale Volume (CNMS) クライアント

米国株の日次ショートボリュームを FINRA の CDN から取得する。認証・APIキー不要。

⚠️ 本データは FINRA 報告分（Off-Exchange。TRF/ADF/ORF 経由。ダークプール等を含む）のみで、
   米国市場全体ではない。したがって比率は必ず同ファイル内の TotalVolume を分母にすること。
   consolidated volume（Yahoo等の出来高）を分母に使うと分子と対象市場が食い違い、
   指標として成立しない（QCルール1）。

⚠️ Daily Short Volume は「フロー」であって空売り残高（Short Interest）ではない。
   当日中に買い戻された分・マーケットメイクの一時的ショート・顧客の現物売却を
   仲介するショートを含む（QCルール2）。
"""
import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config.settings import DATA_DIR

_CNMS_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{compact}.txt"
_EXPECTED_HEADER = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"

SOURCE_NAME = "FINRA_CNMS"
VENUE_SCOPE = "OFF_EXCHANGE"
REGION = "US"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_MAX_NETWORK_RETRIES = 3          # ネットワークエラーのみリトライ（403/404はしない）
_RETRY_BASE_SECONDS = 1.0         # 指数バックオフの基準
_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_REQUEST_INTERVAL = 0.6   # 連続取得時のリクエスト間隔（秒）


def normalize_date(value) -> str:
    """'YYYYMMDD' / 'YYYY-MM-DD' / date / datetime を ISO 'YYYY-MM-DD' へ正規化する。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()

    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    # 既に ISO 形式であることを検証（不正値を静かに通さない）
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _compact(date_iso: str) -> str:
    """ISO 'YYYY-MM-DD' → URL用 'YYYYMMDD'。"""
    return date_iso.replace("-", "")


def _to_float(value: str) -> Optional[float]:
    """カンマ・空白を除去して float 化する。変換できなければ None。

    FINRA の ShortVolume / TotalVolume は整数ではなく小数を含む
    （例: 211769.129173）。int でキャストしないこと（QCルール9）。
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compute_ratio(
    short_volume: Optional[float],
    reported_total_volume: Optional[float],
) -> Optional[float]:
    """FINRA報告分どうしでショート比率(%)を算出する。

    分子・分母は必ず同一ソース内で完結させる。算出できない場合や
    0〜100% の範囲外になる場合は None を返す（欠損は補間しない）。
    """
    if short_volume is None or not reported_total_volume:
        return None
    ratio = short_volume / reported_total_volume * 100
    if not (0.0 <= ratio <= 100.0):
        return None
    return round(ratio, 4)


def build_record(
    date_iso: str,
    ticker: str,
    short_volume: Optional[float] = None,
    short_exempt_volume: Optional[float] = None,
    reported_total_volume: Optional[float] = None,
    market_codes: Optional[str] = None,
) -> dict:
    """正規化済みレコードを組み立てる。

    取得できなかったフィールドは必ず None を明示的に入れる。
    キー自体が欠落することがないため、下流で KeyError が構造的に発生しない。
    """
    return {
        "Date": date_iso,
        "Ticker": ticker,
        "Region": REGION,
        "Source": SOURCE_NAME,
        "VenueScope": VENUE_SCOPE,
        "ShortVolume": short_volume,
        "ShortExemptVolume": short_exempt_volume,
        "ReportedTotalVolume": reported_total_volume,
        "ShortRatioPct": compute_ratio(short_volume, reported_total_volume),
        "MarketCodes": market_codes,
    }


def parse_cnms_text(text: str, tickers: Optional[list[str]] = None) -> list[dict]:
    """CNMS のパイプ区切りテキストを正規化レコードのリストへ変換する。

    Args:
        text:    ダウンロードしたファイル本文
        tickers: 抽出対象ティッカー。None なら全銘柄（約12,000行）

    Returns:
        build_record() 形式の dict のリスト
    """
    if not text:
        return []

    wanted = set(tickers) if tickers else None
    records: list[dict] = []
    malformed = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Date|"):     # ヘッダ行
            continue
        if line.isdigit():
            # 末尾のレコード件数トレーラ行（例: "12233"）。データ行ではない
            continue

        parts = line.split("|")
        if len(parts) < 5:
            malformed += 1
            continue

        # ティッカーにスラッシュを含むクラス株（BRK/B 等）も
        # 区切り文字が "|" なのでそのまま安全に取り出せる
        ticker = parts[1].strip()
        if wanted is not None and ticker not in wanted:
            continue

        try:
            date_iso = normalize_date(parts[0].strip())
        except ValueError:
            malformed += 1
            continue

        records.append(build_record(
            date_iso=date_iso,
            ticker=ticker,
            short_volume=_to_float(parts[2]),
            short_exempt_volume=_to_float(parts[3]),
            reported_total_volume=_to_float(parts[4]),
            market_codes=parts[5].strip() if len(parts) > 5 else None,
        ))

    if malformed:
        logger.warning(f"CNMS の解析不能行をスキップしました: {malformed}行")
    return records


class FinraShortVolumeClient:
    """FINRA CNMS 日次ショートボリュームの取得クライアント"""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        request_interval: float = _DEFAULT_REQUEST_INTERVAL,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else DATA_DIR / "cache" / "finra"
        self.request_interval = request_interval
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------

    def get_daily_records(
        self,
        target_date,
        tickers: Optional[list[str]] = None,
    ) -> list[dict]:
        """指定日のショートボリュームを返す。非営業日・未公開なら空リスト。"""
        date_iso = normalize_date(target_date)
        text = self.fetch_raw(date_iso)
        if text is None:
            return []

        records = parse_cnms_text(text, tickers=tickers)
        logger.info(f"FINRA CNMS 取得: {date_iso} / {len(records)}銘柄")
        return records

    def get_range_records(
        self,
        from_date,
        to_date,
        tickers: Optional[list[str]] = None,
    ) -> list[dict]:
        """期間内の全営業日を順に取得して連結する（土日はリクエストしない）。

        米国の祝日はカレンダーを持たず、403（Access Denied）で判定する。
        """
        start = datetime.strptime(normalize_date(from_date), "%Y-%m-%d").date()
        end = datetime.strptime(normalize_date(to_date), "%Y-%m-%d").date()

        records: list[dict] = []
        missing_days = 0
        current = start
        while current <= end:
            if current.weekday() >= 5:   # 土日はファイル自体が存在しない
                current += timedelta(days=1)
                continue

            day_records = self.get_daily_records(current.isoformat(), tickers=tickers)
            if day_records:
                records.extend(day_records)
            else:
                missing_days += 1
            current += timedelta(days=1)

        logger.info(
            f"FINRA CNMS 期間取得: {start} → {end} / "
            f"{len(records)}レコード（データ無し {missing_days}日）"
        )
        return records

    def fetch_raw(self, target_date) -> Optional[str]:
        """生ファイル本文を返す。キャッシュ優先。非営業日・未公開は None。"""
        date_iso = normalize_date(target_date)

        cached = self._read_cache(date_iso)
        if cached is not None:
            return cached

        text = self._download(date_iso)
        if text is not None:
            self._write_cache(date_iso, text)
        return text

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _download(self, date_iso: str) -> Optional[str]:
        """CDN からダウンロードする。403/404 は「データなし」として静かに None。"""
        url = _CNMS_URL.format(compact=_compact(date_iso))

        for attempt in range(1, _MAX_NETWORK_RETRIES + 1):
            self._throttle()
            try:
                response = requests.get(
                    url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except requests.RequestException as e:
                # ネットワーク起因のみリトライする
                if attempt >= _MAX_NETWORK_RETRIES:
                    logger.warning(f"FINRA 取得失敗（通信エラー）: {date_iso} / {e}")
                    return None
                wait = _RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.info(f"FINRA 通信エラー、{wait:.1f}秒後に再試行 ({attempt}/{_MAX_NETWORK_RETRIES}): {date_iso}")
                time.sleep(wait)
                continue

            if response.status_code == 200:
                text = response.text
                if not text.startswith("Date|"):
                    logger.warning(
                        f"FINRA の応答が想定形式ではありません: {date_iso} / "
                        f"先頭={text[:60]!r}"
                    )
                    return None
                if not text.startswith(_EXPECTED_HEADER):
                    # 列構成が変わった可能性。処理は継続するが必ず気付けるようにする
                    logger.warning(
                        f"FINRA のヘッダが想定と異なります: {text.splitlines()[0]!r}"
                    )
                return text

            if response.status_code in (403, 404):
                # 非営業日・米国祝日・未公開。エラーではないので静かに終える
                logger.debug(f"FINRA データなし（非営業日か未公開）: {date_iso}")
                return None

            if attempt >= _MAX_NETWORK_RETRIES:
                logger.warning(
                    f"FINRA 取得失敗: {date_iso} / HTTP {response.status_code}"
                )
                return None
            time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

        return None

    def _throttle(self) -> None:
        """連続リクエストの間隔を空ける（バックフィル時の配慮）。"""
        if self.request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if 0 < elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _cache_path(self, date_iso: str) -> Path:
        return self.cache_dir / f"CNMSshvol{_compact(date_iso)}.txt"

    def _read_cache(self, date_iso: str) -> Optional[str]:
        path = self._cache_path(date_iso)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"FINRA キャッシュ読み込み失敗: {path} / {e}")
            return None

    def _write_cache(self, date_iso: str, text: str) -> None:
        """成功した取得のみキャッシュする（失敗を焼き付けない）。"""
        path = self._cache_path(date_iso)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            logger.warning(f"FINRA キャッシュ書き込み失敗: {path} / {e}")
