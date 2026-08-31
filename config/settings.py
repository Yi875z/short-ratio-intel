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
# v2 は x-api-key ヘッダ方式（有効期限なし）。v1 のメール+パスワードによる
# refreshToken → idToken の交換は不要になった。
#
# ⚠️ 契約プランで叩けるエンドポイントが変わる。2026-08-29 に実キーで実測した結果:
#   Light で 200: /equities/master, /equities/bars/daily, /indices/bars/daily/topix,
#                 /markets/calendar, /equities/investor-types
#   Light で 403: /markets/short-ratio（業種別空売り比率）, /indices/bars/daily（業種別指数）
# したがって空売り比率そのものは J-Quants からは取れない。従来どおり JPX 公式PDF
# （jpx_pdf_client.py）が正で、J-Quants は騰落銘柄数・TOPIX・営業日カレンダーに使う。
JQUANTS_API_KEY: str = os.getenv("JQUANTS_API_KEY", "")
JQUANTS_BASE_URL: str = "https://api.jquants.com"  # v1 時代の名残。現在は未使用
JQUANTS_API_BASE_URL: str = os.getenv("JQUANTS_API_BASE_URL", "https://api.jquants.com/v2")
JQUANTS_REQUEST_TIMEOUT_SEC: int = int(os.getenv("JQUANTS_REQUEST_TIMEOUT_SEC", "60"))
# Light は 60 リクエスト/分。バックフィルで上限に触れないよう最短間隔を空ける。
JQUANTS_MIN_REQUEST_INTERVAL_SEC: float = float(
    os.getenv("JQUANTS_MIN_REQUEST_INTERVAL_SEC", "1.05")
)
JQUANTS_MAX_RETRIES: int = int(os.getenv("JQUANTS_MAX_RETRIES", "3"))

# ---- Gemini ----
# Free Tier の 20 req/日は GenerateRequestsPerDayPerProjectPerModel-FreeTier、
# つまり「モデル単位」の枠。枯渇時はモデルを変えれば別枠で即復旧できる。
#
# 経緯:
#   2026-07-25 3.5-flash が枯渇し 429 → 3.6-flash へ移行
#   2026-08-23 3.7-flash へ移行
#   2026-08-24 3.7 が本番で 600秒超 → 504 → SDK の内部リトライが日次枠を食い潰し、
#              直後の定時実行が 429 で全滅して AIレポートが欠落 → 3.6 へ差し戻し
#   2026-08-25 内部リトライ停止・日次枠での自動退避を実装。破滅的な連鎖が起きなくなったため、
#              新しい 3.7 の質を実運用で評価する目的で再び 3.7 を先頭に置く。
#              失敗しても GEMINI_FALLBACK_MODELS へ退避してレポート自体は出る。
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# リポジトリ側の正。ここが唯一の既定値で、GitHub Actions の workflow には
# あえて GEMINI_MODEL を置いていない（二重管理で「既定だけ直して本番が変わらない」
# 事故が起きるため）。環境変数での上書きは Streamlit Cloud Secrets 等の
# 緊急避難用に残してあるが、上書き時は起動ログに警告を出して可視化する。
GEMINI_MODEL_DEFAULT: str = "gemini-3.7-flash"
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL") or GEMINI_MODEL_DEFAULT
GEMINI_MODEL_IS_OVERRIDDEN: bool = GEMINI_MODEL != GEMINI_MODEL_DEFAULT

# 日次クォータ（RPD）枯渇や 504 の連続時に順に切り替える退避モデル。
# RPD はモデル単位の枠なので、待つのではなく別モデルへ移るのが最速の復旧になる。
# 3.6-flash は本番同等の入力で 61.7秒・スキーマ検証通過を実測済み（2026-08-25）。
GEMINI_FALLBACK_MODELS: list[str] = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash").split(",")
    if m.strip()
]

# 1リクエストの上限秒数。SDK 既定の 600秒は「内部リトライ込みの総予算」なので、
# 遅いモデルに当たると1回の generate_content が何度もクォータを消費する。
# ここを短く固定し、かつ SDK 内部リトライを無効化して「1呼び出し=1リクエスト」にする。
#
# ⚠️ この値は daily_fetch.yml の timeout-minutes と連動する。
#    最悪ケース = モデル数 × MAX_RETRIES(3) × この秒数。
#    180秒なら 3モデルで約27分＋取得処理となるため、job 側は 35分を確保してある。
#    ここを伸ばすなら workflow の timeout-minutes も必ず一緒に伸ばすこと。
#    伸ばし忘れると、退避モデルへ到達する前に job が打ち切られてレポートが欠落する。
#    実測の目安: 3.6-flash 61.7秒 / 3.7-flash は調子が良い日で 85.5秒。
GEMINI_REQUEST_TIMEOUT_SEC: int = int(os.getenv("GEMINI_REQUEST_TIMEOUT_SEC", "180"))

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
