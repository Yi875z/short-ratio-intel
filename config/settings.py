"""
アプリケーション共通設定
.env から値を読み込む
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# プロジェクトルートの .env を読み込む
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---- J-Quants ----
JQUANTS_API_KEY: str = os.getenv("JQUANTS_API_KEY", "")
JQUANTS_BASE_URL: str = "https://api.jquants.com"

# ---- Gemini ----
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = "gemini-3-flash-preview"

# ---- Slack ----
SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

# ---- ニュース検索 ----
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

# ---- データ・DB ----
DATA_DIR: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
DB_PATH: Path = BASE_DIR / os.getenv("DB_PATH", "data/short_ratio.db")
REPORTS_DIR: Path = BASE_DIR / os.getenv("REPORTS_DIR", "data/reports")
KNOWLEDGE_DIR: Path = BASE_DIR / "src" / "knowledge" / "files"

# ---- ログ ----
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ---- 分析閾値 ----
ANOMALY_DOD_THRESHOLD: float = 3.0    # 前日比±3pt超でアラート
ANOMALY_ZSCORE_THRESHOLD: float = 2.0  # Zスコア±2超でアラート
HISTORY_DAYS_FOR_ZSCORE: int = 30     # Zスコア計算に使う過去日数

# ---- 現在のマクロ背景（Step 0プロトコル） ----
# ⚠️ 重要: 相場環境が変化したら必ずここを更新すること
CURRENT_MACRO_CONTEXT: str = """
主要マクロ背景（2026年2月28日〜継続中）:
米国・イスラエル vs イランの軍事紛争

影響チェーン:
1. ホルムズ海峡リスク → 原油供給不安 → WTI高止まり
2. 原油高 → インフレ再燃懸念 → FRBタカ派化圧力
3. スタグフレーション懸念 → リスクオフ → 日本株下押し
4. 停戦期待ニュース → 逆方向の急激なリスクオン（ショートカバー主導）

空売り比率との対応:
- 紛争激化・ホルムズ懸念強化 → 空売り比率上昇圧力
- 停戦報道・協議進展 → 空売り比率低下・ショートカバー
- ホルムズ封鎖継続×停戦延長の複合状態 → 原油高止まり×長期金利高止まり継続

過去年パターン汚染注意:
⚠️ 2025年4月2日の「トランプ関税ショック」は2026年には不適用
⚠️ 2026年4月の数値急変はイラン情勢コンテキストで解釈すること
"""
