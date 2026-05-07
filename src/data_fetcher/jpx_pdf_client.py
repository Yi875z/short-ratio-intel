"""
JPX公表PDFから空売り集計データを取得するクライアント。
"""
from __future__ import annotations

import re
import zlib
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sectors import SECTORS_S33

_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/short-selling/"
_HOST = "https://www.jpx.co.jp"

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
        """一覧ページから日付・種別に対応するPDF URLを引く。"""
        yymmdd = datetime.strptime(target_date, "%Y-%m-%d").strftime("%y%m%d")
        filename = f"{yymmdd}-{kind}.pdf"
        return self._get_pdf_url_map().get((target_date, kind)) or self._get_pdf_url_map().get((filename, kind))

    def _get_pdf_url_map(self) -> dict[tuple[str, str], str]:
        if self._pdf_url_cache is not None:
            return self._pdf_url_cache

        try:
            resp = requests.get(_INDEX_URL, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"JPX空売り集計ページの取得に失敗しました: {e}")
            self._pdf_url_cache = {}
            return self._pdf_url_cache

        soup = BeautifulSoup(resp.text, "html.parser")
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

        self._pdf_url_cache = pdf_map
        return self._pdf_url_cache

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
