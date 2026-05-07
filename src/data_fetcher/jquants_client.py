"""
空売り比率クライアント
stock-marketdata.com スクレイピング版
"""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sectors import SECTORS_S33

_SCRAPE_URL = "https://stock-marketdata.com/karauri.html"

# サイト表記（中点なし・省略形） → S33コード
_SITE_NAME_TO_S33: dict[str, str] = {
    "水産農林業":      "0050",
    "鉱業":           "1050",
    "建設業":         "2050",
    "食料品":         "3050",
    "繊維製品":       "3100",
    "パルプ紙":       "3150",
    "化学":           "3200",
    "医薬品":         "3250",
    "石油石炭製品":    "3300",
    "ゴム製品":       "3350",
    "ガラス土石":     "3400",
    "鉄鋼":          "3450",
    "非鉄金属":       "3500",
    "金属製品":       "3550",
    "機械":           "3600",
    "電気機器":       "3650",
    "輸送用機器":     "3700",
    "精密機器":       "3750",
    "その他製品":     "3800",
    "電気ガス業":     "4050",
    "陸運業":         "5050",
    "海運業":         "5100",
    "空運業":         "5150",
    "倉庫運輸関連業":  "5200",
    "情報通信業":     "5250",
    "卸売業":         "6050",
    "小売業":         "6100",
    "銀行業":         "7050",
    "証券商品先物":    "7100",
    "保険業":         "7150",
    "その他金融業":    "7200",
    "不動産業":       "8050",
    "サービス業":     "9050",
}

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
            i: _SITE_NAME_TO_S33[h]
            for i, h in enumerate(headers)
            if i > 0 and h in _SITE_NAME_TO_S33
        }
        unknown = [h for i, h in enumerate(headers) if i > 0 and h not in _SITE_NAME_TO_S33]
        if unknown:
            logger.warning(f"マッピング未定義の業種名: {unknown}")

        result = []
        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells:
                continue
            norm_date = cells[0].replace("/", "-")  # "2026/04/24" → "2026-04-24"

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

            result.append({
                "Date": cells[0].replace("/", "-"),
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
                second = ths[1].get_text(strip=True)
                if second in _SITE_NAME_TO_S33:
                    return table
        return None

    @staticmethod
    def _find_market_short_ratio_table(soup: BeautifulSoup):
        """東証全体の時系列テーブルを探す。"""
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.select("thead th")]
            if headers[:4] == ["日付", "空売り比率", "前日比", "売買代金合計"]:
                return table
        return None

    @staticmethod
    def _normalize_date(d: str) -> str:
        d = d.replace("-", "").replace("/", "")
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        raise ValueError(f"不正な日付フォーマット: {d}")
