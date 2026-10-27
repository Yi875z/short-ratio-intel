"""
JPX業種別PDFの文字対応表（ToUnicode CMap）の読み取りに対する回帰テスト。

2026-10-26 分から、PDF の対応表が bfchar（1文字ずつ）ではなく bfrange（範囲指定・配列形式）で
書かれるようになった。旧実装は bfchar の形しか読まず、業種名が「0%」「45」などの数字に化けて
列がずれ、全業種が「範囲外の比率」として破棄された（10/27 の業種別データが欠測）。
"""
import zlib

from src.data_fetcher import jpx_pdf_client
from src.data_fetcher.jpx_pdf_client import (
    JPXShortSellingClient,
    _PDFTextExtractor,
    _SECTOR_ORDER,
)


def _pdf_with_cmap(cmap_body: str) -> bytes:
    stream = zlib.compress(f"begincmap\n{cmap_body}\nendcmap".encode("latin1"))
    return b"stream\n" + stream + b"\nendstream"


def test_bfrangeの配列形式を読む():
    pdf = _pdf_with_cmap(
        "1 beginbfrange\n<0000> <0002> [<0020> <6C34> <7523>]\nendbfrange"
    )
    cmap = _PDFTextExtractor._extract_cmap(pdf)
    assert cmap[0x0000] == " "
    assert cmap[0x0001] == "水"
    assert cmap[0x0002] == "産"


def test_bfrangeの開始値形式は連番で展開する():
    pdf = _pdf_with_cmap("1 beginbfrange\n<3EDC> <3EDF> <0030>\nendbfrange")
    cmap = _PDFTextExtractor._extract_cmap(pdf)
    assert [cmap[c] for c in range(0x3EDC, 0x3EE0)] == ["0", "1", "2", "3"]


def test_bfrangeの範囲を1対1の対応と誤読しない():
    # 旧実装は「<0000> <0002>」を 0x0000→U+0002 のように読んでいた
    pdf = _pdf_with_cmap(
        "1 beginbfrange\n<0000> <0002> [<0020> <6C34> <7523>]\nendbfrange"
    )
    cmap = _PDFTextExtractor._extract_cmap(pdf)
    assert 0x6C34 not in cmap
    assert 0x0020 not in cmap or cmap[0x0020] != "水"


def test_bfcharは従来どおり読む():
    pdf = _pdf_with_cmap("2 beginbfchar\n<0B53> <4E07>\n<0B59> <4E0D>\nendbfchar")
    cmap = _PDFTextExtractor._extract_cmap(pdf)
    assert cmap[0x0B53] == "万"
    assert cmap[0x0B59] == "不"


def test_その他33業種外のラベルの数字を値として拾わない(monkeypatch):
    rows = [
        f"{name} 1,000 50.0% 400 40.0% 100 10.0% 1,500" for _, name in _SECTOR_ORDER
    ]
    text = "分空売り業種別集計 【単位：百万円】 " + " ".join(rows)
    assert "その他（33業種外）" in text

    client = JPXShortSellingClient()
    monkeypatch.setattr(client, "_download_pdf", lambda *_: b"dummy")
    monkeypatch.setattr(jpx_pdf_client._PDFTextExtractor, "extract", staticmethod(lambda _: text))

    records = client.get_sector_breakdown_by_date("2026-10-27")
    assert len(records) == len(_SECTOR_ORDER)
    assert all(r["ShortRatioPct"] == 33.33 for r in records)
    assert records[-1]["TotalVolumeVa"] == 1500
