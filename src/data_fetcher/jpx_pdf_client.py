"""
JPX公表PDFから空売り集計データを取得するクライアント。
"""
from __future__ import annotations

import re
import zlib
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sectors import SECTORS_S33

_DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/short-selling/"
_HOST = "https://www.jpx.co.jp"

# 一覧ページには直近2営業日ぶんしかPDFが載らない（2026-09-03 実測）。
# それより前の日は月別アーカイブページに移る。12ヶ月ぶんが保持されており、
# 01 が前月、12 が13ヶ月前に対応する（同日実測: 01=2026-08 … 12=2025-09）。
# 13以降は404。欠測に気づいたら、まずここを見に行くこと。
_ARCHIVE_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/short-selling/"
    "00-archives-{number:02d}.html"
)
_ARCHIVE_PAGE_MAX = 12

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_SECTOR_ORDER = [
    ("0050", "水産・農林業"),
    ("1050", "鉱業"),
    ("2050", "建設業"),
    ("3050", "食料品"),
    ("3100", "繊維製品"),
    ("3150", "パルプ・紙"),
    ("3200", "化学"),
    ("3250", "医薬品"),
    ("3300", "石油・石炭製品"),
    ("3350", "ゴム製品"),
    ("3400", "ガラス・土石製品"),
    ("3450", "鉄鋼"),
    ("3500", "非鉄金属"),
    ("3550", "金属製品"),
    ("3600", "機械"),
    ("3650", "電気機器"),
    ("3700", "輸送用機器"),
    ("3750", "精密機器"),
    ("3800", "その他製品"),
    ("4050", "電気・ガス業"),
    ("5050", "陸運業"),
    ("5100", "海運業"),
    ("5150", "空運業"),
    ("5200", "倉庫・運輸関連業"),
    ("5250", "情報・通信業"),
    ("6050", "卸売業"),
    ("6100", "小売業"),
    ("7050", "銀行業"),
    ("7100", "証券、商品先物取引業"),
    ("7150", "保険業"),
    ("7200", "その他金融業"),
    ("8050", "不動産業"),
    ("9050", "サービス業"),
    ("9999", "その他（33業種外）"),
]


class JPXShortSellingClient:
    """JPXの空売り集計PDFを取得・解析する。"""

    def __init__(self) -> None:
        self._pdf_url_cache: dict[tuple[str, str], str] | None = None
        # アーカイブは月単位で読み込み、読んだ月を覚えて無駄打ちを避ける。
        self._archive_url_cache: dict[tuple[str, str], str] = {}
        self._archive_months_loaded: set[str] = set()
        self._archive_scanned_all = False

    def get_market_breakdown_by_date(self, target_date: str) -> dict | None:
        """指定日の東証全体PDF（*-m.pdf）から内訳を取得する。"""
        target_date = self._normalize_date(target_date)
        pdf = self._download_pdf(target_date, "m")
        if not pdf:
            return None

        text = _PDFTextExtractor.extract(pdf)
        values = _numbers_from_text(text)
        amount_values = [v for v in values if "," in v]
        if len(amount_values) < 4:
            logger.warning(f"JPX市場全体PDFの数値解析に失敗しました: {target_date}")
            return None

        # 実注文・価格規制あり・価格規制なし・合計の4金額をPDF本文から抽出する。
        actual_va = _to_number(amount_values[0])
        short_with_va = _to_number(amount_values[1])
        short_without_va = _to_number(amount_values[2])
        total_volume_va = _to_number(amount_values[3])
        total_short_va = short_with_va + short_without_va
        short_ratio_pct = _safe_ratio(total_short_va, total_volume_va)

        return {
            "Date": target_date,
            "SellExShortVa": actual_va,
            "ShrtWithResVa": short_with_va,
            "ShrtNoResVa": short_without_va,
            "TotalShortVa": total_short_va,
            "TotalVolumeVa": total_volume_va,
            "ShortRatioPct": short_ratio_pct,
            "DodChange": None,
        }

    def get_sector_breakdown_by_date(self, target_date: str) -> list[dict]:
        """指定日の業種別PDF（*-g.pdf）から33業種＋その他の内訳を取得する。"""
        target_date = self._normalize_date(target_date)
        pdf = self._download_pdf(target_date, "g")
        if not pdf:
            return []

        text = _PDFTextExtractor.extract(pdf)
        values = _numbers_from_text(text)
        needed = len(_SECTOR_ORDER) * 7
        if len(values) < needed:
            logger.warning(
                f"JPX業種別PDFの数値解析に失敗しました: {target_date} "
                f"values={len(values)} needed={needed}"
            )
            return []

        records = []
        values = values[:needed]
        for idx, (s33_code, sector_name) in enumerate(_SECTOR_ORDER):
            row = values[idx * 7:(idx + 1) * 7]
            actual_va = _to_number(row[0])
            short_with_va = _to_number(row[2])
            short_without_va = _to_number(row[4])
            total_volume_va = _to_number(row[6])
            total_short_va = short_with_va + short_without_va
            short_ratio_pct = _safe_ratio(total_short_va, total_volume_va)

            records.append({
                "Date": target_date,
                "S33": s33_code,
                "SectorName": SECTORS_S33.get(s33_code, sector_name),
                "SellExShortVa": actual_va,
                "ShrtWithResVa": short_with_va,
                "ShrtNoResVa": short_without_va,
                "TotalShortVa": total_short_va,
                "TotalVolumeVa": total_volume_va,
                "ShortRatioPct": short_ratio_pct,
            })

        # 空売り比率は定義上 0〜100% の範囲に収まる。
        # それを外れるレコードが1件でもあればパース失敗とみなして全件破棄し、
        # 呼び出し元がフォールバック（stock-marketdata.com）を使えるようにする。
        invalid = [r for r in records if not (0.0 <= r["ShortRatioPct"] <= 100.0)]
        if invalid:
            names = [r["SectorName"] for r in invalid]
            ratios = [r["ShortRatioPct"] for r in invalid]
            logger.error(
                f"JPX業種別PDF パース異常: {target_date} — "
                f"範囲外の比率を検出したため全件破棄します。"
                f" 対象業種={names}, 比率={ratios}"
            )
            return []

        return records

    def get_available_dates(self, limit: int | None = None) -> list[str]:
        """一覧ページに業種別PDFが載っている日付を新しい順に返す。

        取得対象日の候補を stock-marketdata スクレイパーだけに頼ると、先方のHTML変更で
        候補が空になった瞬間に、生きているこのPDF経路まで一度も呼ばれなくなる。
        公式側からも候補日を出せるようにしておくための入口。
        """
        dates = sorted(
            {
                key
                for key, kind in self._get_pdf_url_map()
                if kind == "g" and _DATE_KEY_RE.match(key)
            },
            reverse=True,
        )
        return dates[:limit] if limit else dates

    def _download_pdf(self, target_date: str, kind: str) -> bytes | None:
        yymmdd = datetime.strptime(target_date, "%Y-%m-%d").strftime("%y%m%d")
        url = self._find_pdf_url(target_date, kind)
        if not url:
            logger.warning(f"JPX PDFリンクが一覧ページに見つかりません: {yymmdd}-{kind}.pdf")
            return None

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            if resp.status_code == 404:
                logger.warning(f"JPX PDFが見つかりません: {url}")
                return None
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"JPX PDF取得に失敗しました: {url} / {e}")
            return None
        return resp.content

    def _find_pdf_url(self, target_date: str, kind: str) -> str | None:
        """日付・種別に対応するPDF URLを引く。

        一覧ページ（直近2営業日）に無ければ月別アーカイブを見に行く。
        欠測日を「もう取れない」と諦めないための経路であり、
        過去データの取り直しはここに依存している。
        """
        yymmdd = datetime.strptime(target_date, "%Y-%m-%d").strftime("%y%m%d")
        filename = f"{yymmdd}-{kind}.pdf"

        index_map = self._get_pdf_url_map()
        url = index_map.get((target_date, kind)) or index_map.get((filename, kind))
        if url:
            return url

        archive_map = self._get_archive_url_map(target_date)
        return archive_map.get((target_date, kind)) or archive_map.get((filename, kind))

    def _get_pdf_url_map(self) -> dict[tuple[str, str], str]:
        if self._pdf_url_cache is not None:
            return self._pdf_url_cache

        try:
            resp = requests.get(_INDEX_URL, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            # ⚠️ ここで空の辞書をキャッシュしてはいけない。
            # 一度の通信失敗を覚え込むと、そのインスタンスは以後すべての日付で
            # PDFを見つけられず、全日が stock-marketdata へフォールバックする。
            # スクレイパーは内訳を持たない（0を返す）ため、既存の正しい内訳が
            # 0で上書きされてデータが失われる（2026-09-01 に5営業日ぶん破壊された）。
            # キャッシュせずに返し、次の呼び出しで取得し直させる。
            logger.warning(f"JPX空売り集計ページの取得に失敗しました（次回再試行）: {e}")
            return {}

        self._pdf_url_cache = self._parse_pdf_links(resp.text)
        return self._pdf_url_cache

    # ------------------------------------------------------------------
    # 月別アーカイブ
    # ------------------------------------------------------------------
    def _get_archive_url_map(self, target_date: str) -> dict[tuple[str, str], str]:
        """対象日の属する月のアーカイブを読み込み、累積したURL表を返す。"""
        month = target_date[:7]
        if self._archive_scanned_all or month in self._archive_months_loaded:
            return self._archive_url_cache

        number = self._archive_page_number(month)
        if number and self._load_archive_page(number, expect_month=month):
            return self._archive_url_cache

        # 採番の推測が外れた（JPX側の並びが変わった等）。12ページなら総当たりで足りる。
        logger.info(f"アーカイブの採番推測が外れたため全ページを走査します: {month}")
        for candidate in range(1, _ARCHIVE_PAGE_MAX + 1):
            self._load_archive_page(candidate)
        self._archive_scanned_all = True
        return self._archive_url_cache

    @staticmethod
    def _archive_page_number(month: str, today: date | None = None) -> int | None:
        """「YYYY-MM」が何番のアーカイブページかを今月からの差で求める。

        01 が前月。当月（差0）は一覧ページ側にあるためアーカイブには無い。
        推測が外れても _load_archive_page が中身で検証して落とす。
        """
        today = today or date.today()
        try:
            year, mon = int(month[:4]), int(month[5:7])
        except (TypeError, ValueError):
            return None
        distance = (today.year * 12 + today.month) - (year * 12 + mon)
        return distance if 1 <= distance <= _ARCHIVE_PAGE_MAX else None

    def _load_archive_page(self, number: int, expect_month: str | None = None) -> bool:
        """アーカイブ1ページを読み込む。期待した月が入っていれば True。"""
        url = _ARCHIVE_URL.format(number=number)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            # 一覧ページと同じ理由で、失敗は覚え込まない。
            logger.warning(f"JPXアーカイブ{number:02d}の取得に失敗しました（次回再試行）: {e}")
            return False

        pdf_map = self._parse_pdf_links(resp.text)
        if not pdf_map:
            return False

        months = {key[:7] for key, _ in pdf_map if _DATE_KEY_RE.match(key)}
        # 取れたぶんは期待外れでもキャッシュする（同じページを二度読まないため）。
        self._archive_url_cache.update(pdf_map)
        self._archive_months_loaded |= months
        return expect_month is None or expect_month in months

    @staticmethod
    def _parse_pdf_links(html: str) -> dict[tuple[str, str], str]:
        """ページ内の `YYMMDD-{m,g}.pdf` リンクを日付キーの表にする。"""
        soup = BeautifulSoup(html, "html.parser")
        pdf_map: dict[tuple[str, str], str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            match = re.search(r"(\d{6})-([mg])\.pdf$", href)
            if not match:
                continue

            yymmdd, kind = match.groups()
            yyyy_mm_dd = datetime.strptime(yymmdd, "%y%m%d").strftime("%Y-%m-%d")
            pdf_map[(yyyy_mm_dd, kind)] = urljoin(_HOST, href)
            pdf_map[(f"{yymmdd}-{kind}.pdf", kind)] = urljoin(_HOST, href)
        return pdf_map

    @staticmethod
    def _normalize_date(d: str) -> str:
        d = d.replace("-", "").replace("/", "")
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        raise ValueError(f"不正な日付フォーマット: {d}")


class _PDFTextExtractor:
    """JPX PDF向けの軽量テキスト抽出器。"""

    @staticmethod
    def extract(pdf_bytes: bytes) -> str:
        cmap = _PDFTextExtractor._extract_cmap(pdf_bytes)
        parts = []

        for stream in _PDFTextExtractor._decompressed_streams(pdf_bytes):
            if not stream.lstrip().startswith(b"/") or b"BT" not in stream:
                continue
            # Walk matches while tracking what bytes lie between them.
            # Adjacent hex/string tokens with only whitespace between them are digits
            # of the same number; tokens separated by PDF operators (letters) are
            # different cells and need a space boundary.
            _OPERATOR = re.compile(rb"[A-Za-z]")
            last_end = 0
            for match in re.finditer(rb"<([0-9A-Fa-f]+)>|\(([^)]*)\)", stream):
                between = stream[last_end:match.start()]
                has_operator = bool(_OPERATOR.search(between))
                if match.group(1):
                    text = _PDFTextExtractor._decode_hex(match.group(1), cmap)
                else:
                    text = match.group(2).decode("latin1", errors="ignore")
                text = text.strip()
                if text:
                    if has_operator or not parts:
                        parts.append(text)
                    else:
                        parts[-1] += text  # same cell: concatenate directly
                last_end = match.end()

        return " ".join(parts)

    @staticmethod
    def _decompressed_streams(pdf_bytes: bytes) -> list[bytes]:
        streams = []
        for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
            raw = match.group(1)
            try:
                streams.append(zlib.decompress(raw))
            except zlib.error:
                continue
        return streams

    @staticmethod
    def _extract_cmap(pdf_bytes: bytes) -> dict[int, str]:
        cmap = {}
        for stream in _PDFTextExtractor._decompressed_streams(pdf_bytes):
            if b"begincmap" not in stream:
                continue
            text = stream.decode("latin1", errors="ignore")
            for src, dst in re.findall(r"<([0-9A-Fa-f]{4})>\s+<([0-9A-Fa-f]{4})>", text):
                cmap[int(src, 16)] = chr(int(dst, 16))
        return cmap

    @staticmethod
    def _decode_hex(hex_bytes: bytes, cmap: dict[int, str]) -> str:
        raw = bytes.fromhex(hex_bytes.decode("ascii"))
        chars = []
        for idx in range(0, len(raw), 2):
            code = int.from_bytes(raw[idx:idx + 2], "big")
            chars.append(cmap.get(code, chr(code) if 32 <= code < 127 else ""))
        return "".join(chars)


def _numbers_from_text(text: str) -> list[str]:
    text = re.sub(r",\s+", ",", text)
    text = re.sub(r"\.\s+", ".", text)
    return re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?%?", text)


def _to_number(value: str) -> float:
    return float(value.replace(",", "").replace("%", ""))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)
