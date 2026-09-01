"""
月次検証の通知文のテスト。

狙いは「オオカミ少年にしない」こと。通知が毎月『差が出た』と鳴り続けると読まれなくなる。
条件付き標本（n≈400・符号が反転する）を全営業日の結果と同列に数えないことを固定する。
"""
import pytest

from scripts import notify_validation_summary as notifier


def _report(full_rows: str, conditional_rows: str = "") -> str:
    return f"""# Phase 1 検証レポート

- 対象期間: 2025-08-29 〜 2026-09-01
- 観測数: 8,330行

## T+1 営業日

### 全営業日

| 特徴量 | IC(日次平均) | t値 | 上位20%平均 | 下位20%平均 | スプレッド | 上位勝率 | 下位勝率 |
|---|---:|---:|---:|---:|---:|---:|---:|
{full_rows}

### 空売り比率が異常に高かった日のみ（Zスコア ≥ +1.0）

| 特徴量 | IC(日次平均) | t値 | 上位20%平均 | 下位20%平均 | スプレッド | 上位勝率 | 下位勝率 |
|---|---:|---:|---:|---:|---:|---:|---:|
{conditional_rows}
"""


def _row(name, t, spread):
    return (
        f"| {name} | -0.0100 | {t} | +0.100% | -0.100% | "
        f"**{spread:+.3f}pt** | 50.0% | 46.0% |"
    )


def _patch(monkeypatch, tmp_path, markdown):
    path = tmp_path / "validation_phase1.md"
    path.write_text(markdown, encoding="utf-8")
    monkeypatch.setattr(notifier, "_REPORT_PATH", path)


def test_有意な項目が無ければ判断維持と伝える(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, _report(_row("VWAP超え銘柄比率", "-1.02", 0.092)))
    summary = notifier.build_summary()

    assert "有意な予測力なし" in summary
    assert "状態分類を作らない判断を維持" in summary
    assert "0 / 1" in summary


def test_条件付き標本の大きな差では鳴らさない(monkeypatch, tmp_path):
    """n が小さく符号が不安定な条件付き結果を根拠にしない。"""
    _patch(monkeypatch, tmp_path, _report(
        full_rows=_row("VWAP超え銘柄比率", "-1.02", 0.092),
        conditional_rows=_row("VWAP超え銘柄比率", "—", -0.599),
    ))
    summary = notifier.build_summary()

    assert "有意な予測力なし" in summary


def test_t値が足りない大きなスプレッドでは鳴らさない(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, _report(_row("高値引け比率", "-1.34", 0.500)))
    assert "有意な予測力なし" in notifier.build_summary()


def test_有意かつ符号が一貫していれば名前を挙げる(monkeypatch, tmp_path):
    markdown = _report(_row("VWAP超え銘柄比率", "+2.50", 0.400))
    # 別ホライズンでも同じ符号
    markdown += """
## T+5 営業日

### 全営業日

| 特徴量 | IC(日次平均) | t値 | 上位20%平均 | 下位20%平均 | スプレッド | 上位勝率 | 下位勝率 |
|---|---:|---:|---:|---:|---:|---:|---:|
""" + _row("VWAP超え銘柄比率", "+2.10", 0.300) + "\n"
    _patch(monkeypatch, tmp_path, markdown)

    summary = notifier.build_summary()
    assert "符号が一貫した有意項目あり" in summary
    assert "VWAP超え銘柄比率" in summary
    assert "多重検定" in summary


def test_有意でも符号が反転していればノイズ扱いにする(monkeypatch, tmp_path):
    markdown = _report(_row("VWAP超え銘柄比率", "+2.50", 0.400))
    markdown += """
## T+5 営業日

### 全営業日

| 特徴量 | IC(日次平均) | t値 | 上位20%平均 | 下位20%平均 | スプレッド | 上位勝率 | 下位勝率 |
|---|---:|---:|---:|---:|---:|---:|---:|
""" + _row("VWAP超え銘柄比率", "+2.10", -0.300) + "\n"
    _patch(monkeypatch, tmp_path, markdown)

    summary = notifier.build_summary()
    assert "符号が反転" in summary
    assert "ノイズの疑い" in summary


def test_レポートが無ければその旨を伝える(monkeypatch, tmp_path):
    monkeypatch.setattr(notifier, "_REPORT_PATH", tmp_path / "missing.md")
    assert "生成されていません" in notifier.build_summary()


def test_書式が変わって読めなければ黙らず警告する(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, "# 見出しだけで表が無いレポート")
    assert "読めませんでした" in notifier.build_summary()
