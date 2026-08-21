"""
空売り比率クライアント
stock-marketdata.com スクレイピング版
"""
import re

import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sectors import SECTORS_S33

_SCRAPE_URL = "https://stock-marketdata.com/karauri.html"

# 取得元は業種名の表記を予告なく変える。2026-08 の変更では「水産農林業」→「水産・農林業」、
# 「証券商品先物」→「証券、商品先物取引業」のように中点・読点が入り、完全一致で引いていた
# 旧マッピングが全滅して3営業日ぶんの欠測を出した。区切り文字を落とした正規化名で引くことで
# 中点あり・なしの両表記を吸収する。
_NAME_SEPARATORS = "・、･, 　	"


def _normalize_sector_name(name: str) -> str:
    """業種名から区切り文字と空白を除いた、比較用の文字列を返す。"""
    for ch in _NAME_SEPARATORS:
        name = name.replace(ch, "")
    return name.strip()


# 正規化しても canonical 名（config/sectors.py）と一致しない省略表記だけを別名で補う。
_LEGACY_SITE_ALIASES: dict[str, str] = {
    "ガラス土石": "3400",     # canonical: ガラス・土石製品
    "証券商品先物": "7100",   # canonical: 証券、商品先物取引業
}

_S33_BY_NORMALIZED_NAME: dict[str, str] = {
    _normalize_sector_name(name): code
    for code, name in SECTORS_S33.items()
    if code != "9999"  # 「その他（33業種外）」は取得元の列に存在しない
}
_S33_BY_NORMALIZED_NAME.update(_LEGACY_SITE_ALIASES)


def _lookup_s33(header: str) -> str | None:
    """テーブルのヘッダー表記から S33 コードを引く（未知の表記なら None）。"""
    return _S33_BY_NORMALIZED_NAME.get(_normalize_sector_name(header))


# 日付セルの表記も 2026-08 に "2026/08/21" → "2026年8月21日" へ変わった。両方受ける。
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"),
    re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$"),
)


def _parse_table_date(raw: str) -> str | None:
    """日付セルを "YYYY-MM-DD" に正規化する（解釈できなければ None）。"""
    raw = raw.strip()
    for pattern in _DATE_PATTERNS:
        matched = pattern.match(raw)
        if matched:
            year, month, day = matched.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


class JQuantsClient:
    """空売り比率スクレイパー（stock-marketdata.com）"""

    def get_short_ratio_by_date(self, target_date: str) -> list[dict]:
        """
        指定日の全33業種の空売り比率データを返す。

        Args:
            target_date: "YYYY-MM-DD" または "YYYYMMDD"

        Returns:
            List of dicts: Date, S33, SectorName, ShortRatioPct
        """
        target_date = self._normalize_date(target_date)
        logger.info(f"空売り比率データを取得: {target_date}")

        for day_records in self._fetch_all_rows():
            if day_records and day_records[0]["Date"] == target_date:
                return day_records

        logger.warning(f"{target_date} のデータが見つかりません（非営業日の可能性）")
        return []

    def get_short_ratio_range(
        self,
        s33_code: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        """指定業種コードの期間データを返す。"""
        from_date = self._normalize_date(from_date)
        to_date = self._normalize_date(to_date)
        logger.info(f"期間データ取得: {s33_code} / {from_date} → {to_date}")

        result = []
        for day_records in self._fetch_all_rows():
            for r in day_records:
                if r["S33"] == s33_code and from_date <= r["Date"] <= to_date:
                    result.append(r)
        return result

    def get_recent_days(self, days: int = 5) -> list[dict]:
        """直近N営業日分の全業種データを返す。"""
        logger.info(f"直近{days}営業日分のデータを取得")

        result = []
        seen: set[str] = set()
        for day_records in self._fetch_all_rows():
            if not day_records:
                continue
            d = day_records[0]["Date"]
            if d not in seen:
                seen.add(d)
                result.extend(day_records)
            if len(seen) >= days:
                break
        return result

    def get_market_short_ratio_by_date(self, target_date: str) -> dict | None:
        """指定日の東証全体の空売り比率データを返す。"""
        target_date = self._normalize_date(target_date)
        for row in self._fetch_market_rows():
            if row["Date"] == target_date:
                return row
        logger.warning(f"{target_date} の東証全体データが見つかりません")
        return None

    def get_market_recent_days(self, days: int = 5) -> list[dict]:
        """直近N営業日分の東証全体データを返す。"""
        return self._fetch_market_rows()[:days]

    # ------------------------------------------------------------------

    def _fetch_soup(self) -> BeautifulSoup | None:
        try:
            resp = requests.get(_SCRAPE_URL, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"スクレイピング失敗: {e}")
            return None

        return BeautifulSoup(resp.text, "html.parser")

    def _fetch_all_rows(self) -> list[list[dict]]:
        """ページ全体をスクレイプし、日付ごとにグループ化したリストを返す。"""
        soup = self._fetch_soup()
        if soup is None:
            return []

        table = self._find_short_ratio_table(soup)
        if table is None:
            logger.error("空売り比率テーブルが見つかりません")
            return []

        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not headers or headers[0] != "日付":
            logger.error(f"テーブルヘッダーが期待と異なります: {headers[:5]}")
            return []

        # 列インデックス → S33コード（日付列 index=0 を除く）
        col_s33 = {
            i: code
            for i, h in enumerate(headers)
            if i > 0 and (code := _lookup_s33(h))
        }
        unknown = [h for i, h in enumerate(headers) if i > 0 and not _lookup_s33(h)]
        if unknown:
            logger.warning(f"マッピング未定義の業種名: {unknown}")

        result = []
        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells:
                continue
            norm_date = _parse_table_date(cells[0])
            if norm_date is None:
                continue

            day_records = []
            for col_idx, s33 in col_s33.items():
                if col_idx >= len(cells):
                    continue
                try:
                    ratio = round(float(cells[col_idx].replace("%", "")), 2)
                except ValueError:
                    continue
                day_records.append({
                    "Date": norm_date,
                    "S33": s33,
                    "SectorName": SECTORS_S33.get(s33, f"不明({s33})"),
                    "SellExShortVa": 0,
                    "ShrtWithResVa": 0,
                    "ShrtNoResVa": 0,
                    "TotalShortVa": 0,
                    "TotalVolumeVa": 0,
                    "ShortRatioPct": ratio,
                })
            if day_records:
                result.append(day_records)

        return result

    def _fetch_market_rows(self) -> list[dict]:
        """東証全体の時系列テーブルを取得する。"""
        soup = self._fetch_soup()
        if soup is None:
            return []

        table = self._find_market_short_ratio_table(soup)
        if table is None:
            logger.error("東証全体の空売り比率テーブルが見つかりません")
            return []

        result = []
        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if len(cells) < 4:
                continue

            try:
                ratio = round(float(cells[1].replace("%", "")), 2)
                total_volume = float(cells[3].replace(",", ""))
            except ValueError:
                continue

            dod_change = None
            try:
                dod_change = round(float(cells[2].replace("+", "").replace("%", "")), 2)
            except ValueError:
                pass

            norm_date = _parse_table_date(cells[0])
            if norm_date is None:
                continue

            result.append({
                "Date": norm_date,
                "ShortRatioPct": ratio,
                "DodChange": dod_change,
                "SellExShortVa": 0,
                "ShrtWithResVa": 0,
                "ShrtNoResVa": 0,
                "TotalShortVa": 0,
                "TotalVolumeVa": total_volume,
            })

        return result

    @staticmethod
    def _find_short_ratio_table(soup: BeautifulSoup):
        """33業種列を含むテーブルを探す（先頭th=「日付」かつ2列目が業種名）。"""
        for table in soup.find_all("table"):
            ths = table.select("thead th")
            if len(ths) >= 2 and ths[0].get_text(strip=True) == "日付":
                if _lookup_s33(ths[1].get_text(strip=True)):
                    return table
        return None

    @staticmethod
    def _find_market_short_ratio_table(soup: BeautifulSoup):
        """東証全体の時系列テーブルを探す。"""
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.select("thead th")]
            if len(headers) < 4:
                continue
            if (
                headers[:3] == ["日付", "空売り比率", "前日比"]
                and headers[3].startswith("売買代金")
            ):
                return table
        return None

    @staticmethod
    def _normalize_date(d: str) -> str:
        d = d.replace("-", "").replace("/", "")
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        raise ValueError(f"不正な日付フォーマット: {d}")
