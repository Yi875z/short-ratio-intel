"""
NEOグランドマスター ナレッジファイル読み込みモジュール
"""
from pathlib import Path
from loguru import logger

from config.settings import EXTERNAL_KNOWLEDGE_DIR, KNOWLEDGE_DIR

# ナレッジファイルのマッピング
KNOWLEDGE_FILES = {
    "global_macro":  "01_global_macro.md",
    "jpx_micro":     "02_jpx_micro.md",
    "options_gex":   "03_options_gex.md",
    "quant_psych":   "04_quant_psych.md",
}

EXTERNAL_KNOWLEDGE_FILES = {
    "project_protocol": "00_PROJECT_OPERATING_PROTOCOL.md",
    "market_preview_spec": "01_MARKET_PREVIEW_OUTPUT_SPEC.md",
    "jpx_micro": "02_JPX_Micro_Flows.md",
    "options_gex": "03_Options_and_GEX_Master.md",
    "global_macro": "04_Global_Macro_Dynamics.md",
    "quant_psych": "05_Quant_Tech_Psychology.md",
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


def load_external_knowledge(key: str) -> str:
    """外部ナレッジ（思考データ）を読み込む。

    Supabase(DB) を優先し、無ければローカルファイル（EXTERNAL_KNOWLEDGE_DIR）へ
    フォールバックする。公開リポにファイルを置かずに、クラウド（Streamlit Cloud /
    GitHub Actions）でも知見を反映できるようにするため。
    """
    filename = EXTERNAL_KNOWLEDGE_FILES.get(key)
    if not filename:
        logger.warning(f"不明な外部ナレッジキー: {key}")
        return ""

    # 1) Supabase 優先（クラウド・Actions・DATABASE_URL設定時）
    from src.storage.db import get_knowledge_document

    content = get_knowledge_document(key)
    if content:
        return content

    # 2) ローカルファイルにフォールバック（純ローカル開発・DB未登録時）
    filepath = EXTERNAL_KNOWLEDGE_DIR / filename
    if not filepath.exists():
        logger.warning(
            f"外部ナレッジが DB にもローカルにも見つかりません: {key} ({filepath})"
        )
        return ""

    try:
        return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"外部ナレッジファイル読み込みエラー: {filepath} / {e}")
        return ""


def load_all_external_knowledge() -> dict[str, str]:
    """利用可能な外部ナレッジファイルを辞書で返す。"""
    return {key: load_external_knowledge(key) for key in EXTERNAL_KNOWLEDGE_FILES}


def load_effective_knowledge() -> dict[str, str]:
    """
    レポート生成で使う実効ナレッジを返す。

    同じ領域の外部ナレッジが存在する場合は、ChatGPT 5.5プロジェクト側の
    ファイルを優先し、存在しない場合だけローカル同梱ファイルへ戻す。
    """
    local = load_all_knowledge()
    external = load_all_external_knowledge()

    effective = dict(local)
    for key in ["global_macro", "jpx_micro", "options_gex", "quant_psych"]:
        if external.get(key):
            effective[key] = external[key]

    effective["project_protocol"] = external.get("project_protocol", "")
    effective["market_preview_spec"] = external.get("market_preview_spec", "")
    return effective


def check_knowledge_files() -> dict[str, bool]:
    """ナレッジファイルの存在確認"""
    return {
        key: (KNOWLEDGE_DIR / filename).exists()
        for key, filename in KNOWLEDGE_FILES.items()
    }


def check_external_knowledge_files() -> dict[str, bool]:
    """外部ナレッジファイルの存在確認"""
    return {
        key: (EXTERNAL_KNOWLEDGE_DIR / filename).exists()
        for key, filename in EXTERNAL_KNOWLEDGE_FILES.items()
    }
