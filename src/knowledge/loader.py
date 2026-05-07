"""
NEOグランドマスター ナレッジファイル読み込みモジュール
"""
from pathlib import Path
from loguru import logger

from config.settings import KNOWLEDGE_DIR

# ナレッジファイルのマッピング
KNOWLEDGE_FILES = {
    "global_macro":  "01_global_macro.md",
    "jpx_micro":     "02_jpx_micro.md",
    "options_gex":   "03_options_gex.md",
    "quant_psych":   "04_quant_psych.md",
}


def load_knowledge(key: str) -> str:
    """
    指定キーのナレッジファイルを読み込む。

    Args:
        key: "global_macro" | "jpx_micro" | "options_gex" | "quant_psych"

    Returns:
        ファイルの内容文字列。ファイルが存在しない場合は空文字列。
    """
    filename = KNOWLEDGE_FILES.get(key)
    if not filename:
        logger.warning(f"不明なナレッジキー: {key}")
        return ""

    filepath = KNOWLEDGE_DIR / filename
    if not filepath.exists():
        logger.warning(
            f"ナレッジファイルが見つかりません: {filepath}\n"
            f"  → src/knowledge/files/ に以下のファイルを配置してください:\n"
            f"     {', '.join(KNOWLEDGE_FILES.values())}"
        )
        return f"[{key} ナレッジファイル未配置]"

    try:
        return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"ナレッジファイル読み込みエラー: {e}")
        return ""


def load_all_knowledge() -> dict[str, str]:
    """全ナレッジファイルを読み込んで辞書で返す"""
    return {key: load_knowledge(key) for key in KNOWLEDGE_FILES}


def check_knowledge_files() -> dict[str, bool]:
    """ナレッジファイルの存在確認"""
    return {
        key: (KNOWLEDGE_DIR / filename).exists()
        for key, filename in KNOWLEDGE_FILES.items()
    }
