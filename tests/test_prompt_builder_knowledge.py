"""ナレッジ注入まわり（00ダイジェスト抽出・SQ週の過去事例注入・新フィールド）のテスト。"""
from src.ai_engine import prompt_builder as pb
from src.ai_engine.output_schema import DominantMarketTheme, ReadingReport


_FAKE_PROTOCOL = """# 00_PROTOCOL
## 2. Project Instructions に貼る短縮版
ここはChatGPT運用向けなので抽出しない。

## 9. JPX分析の禁止・推奨表現
売り残高と表現しない。

### 9.1 禁止表現
必ず上がる等は禁止。

## 17. マーケットプレビュー作成ルール
ここも抽出しない。

## 10. 投資主体別の時間差ルール
先週の確定需給として見る。
"""


def test_protocol_digest_extracts_only_analysis_sections():
    digest = pb._extract_protocol_digest(_FAKE_PROTOCOL)
    assert "JPX分析の禁止・推奨表現" in digest
    assert "投資主体別の時間差ルール" in digest
    assert "禁止表現" in digest  # サブセクション（###）も含めて抽出される
    assert "Project Instructions" not in digest
    assert "マーケットプレビュー作成ルール" not in digest


def test_protocol_digest_falls_back_to_clip_when_no_match():
    text = "## 見出しA\n本文\n" * 10
    digest = pb._extract_protocol_digest(text)
    assert "見出しA" in digest  # 抽出0件でも従来クリップで中身は残る


def test_sq_week_case_block_injected_only_on_sq_week(monkeypatch):
    monkeypatch.setattr(pb, "load_external_knowledge", lambda key: "SQ週の教訓テキスト")
    # 2026-06-08はMSQ週（SQ=6/12、ロール=6/10が+5日窓に入る）
    block = pb._build_sq_week_case_block("2026-06-08")
    assert "SQ週の教訓テキスト" in block
    assert "再発防止" in block
    # 2026-06-23前後にSQ・ロールはない → 注入しない
    assert pb._build_sq_week_case_block("2026-06-23") == ""


def test_sq_week_case_block_empty_when_knowledge_missing(monkeypatch):
    monkeypatch.setattr(pb, "load_external_knowledge", lambda key: "")
    assert pb._build_sq_week_case_block("2026-06-08") == ""


def test_theme_flow_classification_defaults_to_unconfirmed():
    theme = DominantMarketTheme(
        theme_name="テスト",
        importance="high",
        status="主テーマ候補",
        evidence=[],
        impact_channels=[],
        related_sectors=[],
        short_ratio_alignment="整合",
        caveat="注記",
    )
    assert theme.flow_classification == "Unconfirmed"


def test_reading_report_new_fields_have_defaults():
    fields = ReadingReport.model_fields
    assert fields["executive_summary"].default == ""
    assert fields["regime"].default == ""
