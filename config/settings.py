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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

# ---- J-Quants ----
JQUANTS_API_KEY: str = os.getenv("JQUANTS_API_KEY", "")
JQUANTS_BASE_URL: str = "https://api.jquants.com"

# ---- Gemini ----
# モデルは .env の GEMINI_MODEL で切替可能（未設定時は gemini-3.7-flash）
# Free Tier の 20 req/日は GenerateRequestsPerDayPerProjectPerModel-FreeTier、
# つまり「モデル単位」の枠。枯渇時はモデルを変えれば別枠で即復旧できる。
# 2026-07-25: JPX_Analysis_System で 3.5-flash が枯渇し 429 になった事例を受けて 3.6-flash へ移行。
# 2026-08-23: 3.7-flash へ移行（退避先は gemini-3.6-flash）。
#   3.7 は 2026-08-13 GA だが翌 08-14 は 504 連発で一度不採用にした経緯がある。
#   08-23 に本番同条件（system=67.8K字 / response_mime_type=application/json /
#   max_output_tokens=32768）で再検証し、API 85.5秒・ReadingReport のスキーマ検証
#   通過を確認して採用。※JSON経路なので「200が返った」だけでは不十分。
#   必ずスキーマ検証（_parse_response）まで通してから採用すること。
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")

# ---- Slack ----
SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

# ---- ニュース検索 ----
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

# ---- データ・DB ----
DATA_DIR: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
DB_PATH: Path = BASE_DIR / os.getenv("DB_PATH", "data/short_ratio.db")
REPORTS_DIR: Path = BASE_DIR / os.getenv("REPORTS_DIR", "data/reports")
KNOWLEDGE_DIR: Path = BASE_DIR / "src" / "knowledge" / "files"
EXTERNAL_KNOWLEDGE_DIR: Path = Path(
    os.getenv("EXTERNAL_KNOWLEDGE_DIR", r"C:\CarSol\knowledgefile")
)

# ---- ログ ----
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ---- 分析閾値 ----
ANOMALY_DOD_THRESHOLD: float = 3.0    # 前日比±3pt超でアラート
ANOMALY_ZSCORE_THRESHOLD: float = 2.0  # Zスコア±2超でアラート
HISTORY_DAYS_FOR_ZSCORE: int = 30     # Zスコア計算に使う過去日数

# ---- 米国ショートフロー（FINRA CNMS）----
# 判定は絶対閾値（50%等）ではなく、銘柄自身の過去分布に対する相対評価で行う。
# 平常時45〜55%の銘柄と25〜35%の銘柄では、同じ52%でも意味が全く異なるため。
US_ZSCORE_WINDOW_SHORT: int = int(os.getenv("US_ZSCORE_WINDOW_SHORT", "20"))
US_ZSCORE_WINDOW_LONG: int = int(os.getenv("US_ZSCORE_WINDOW_LONG", "60"))
# 窓幅に対して必要な最低サンプル比率。下回れば判定せず None を返す（欠損は補間しない）
US_MIN_SAMPLE_COVERAGE: float = float(os.getenv("US_MIN_SAMPLE_COVERAGE", "0.8"))
US_ZSCORE_ALERT_THRESHOLD: float = 2.0    # |z20| がこれを超えたら異常
US_ZSCORE_EXTREME_THRESHOLD: float = 3.0  # |z20| がこれを超えたら極端
US_BACKFILL_DAYS: int = int(os.getenv("US_BACKFILL_DAYS", "250"))  # 初回バックフィルの営業日数

# ---- 市場テーマ判定 ----
MARKET_THEME_MAX_ITEMS: int = int(os.getenv("MARKET_THEME_MAX_ITEMS", "3"))
MARKET_THEME_MIN_SCORE: float = float(os.getenv("MARKET_THEME_MIN_SCORE", "2.0"))
MARKET_NEWS_AUTO_FETCH: bool = _env_bool("MARKET_NEWS_AUTO_FETCH", False)
MARKET_NEWS_MAX_RESULTS: int = int(os.getenv("MARKET_NEWS_MAX_RESULTS", "5"))
MARKET_NEWS_TIMEOUT_SECONDS: int = int(os.getenv("MARKET_NEWS_TIMEOUT_SECONDS", "20"))

# ---- RSSニュース（ロイター/日経/Bloomberg + Google News）----
# 無料・APIキー不要のため既定ON。Tavily(要キー)は任意の補助に降格。
MARKET_NEWS_RSS_ENABLED: bool = _env_bool("MARKET_NEWS_RSS_ENABLED", True)
# レポート対象日から何日さかのぼってニュースを拾うか（Google Newsの日付窓）
MARKET_NEWS_RSS_WINDOW_DAYS: int = int(os.getenv("MARKET_NEWS_RSS_WINDOW_DAYS", "3"))
# 対象日が今日からこの日数以内なら第三者RSS(現在見出しのみ)も併用する
MARKET_NEWS_RSS_RECENT_DAYS: int = int(os.getenv("MARKET_NEWS_RSS_RECENT_DAYS", "4"))
MARKET_NEWS_RSS_MAX_ITEMS: int = int(os.getenv("MARKET_NEWS_RSS_MAX_ITEMS", "16"))
# 著作権配慮: 既定では見出しのみ。Trueのとき短い要約を一時的にプロンプトへ含める
MARKET_NEWS_RSS_INCLUDE_SUMMARY: bool = _env_bool("MARKET_NEWS_RSS_INCLUDE_SUMMARY", False)

# ---- ハウスビュー（運用者の常設の相場観）----
# 最終更新からこの日数を超えたら鮮度警告を出す（自動実行で古い見解を使い続けない）
HOUSE_VIEW_STALE_DAYS: int = int(os.getenv("HOUSE_VIEW_STALE_DAYS", "14"))

# ---- 投資主体別フロー（姉妹プロジェクト jpx-analysis の別Supabaseを読む）----
# jpx-analysis プロジェクトの Supabase URL と KEY（RLS無効の読み取り用）。
# 未設定なら機関フロー突合はスキップ（本アプリは止めない）。秘密は Secrets で管理。
JPX_ANALYSIS_SUPABASE_URL: str = os.getenv("JPX_ANALYSIS_SUPABASE_URL", "")
JPX_ANALYSIS_SUPABASE_KEY: str = os.getenv("JPX_ANALYSIS_SUPABASE_KEY", "")

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
