"""
米国ショートフロー分析の監視ユニバース

レバレッジETF（SOXL / SOXS 等）は対象外。日次リバランスに伴う機械的な
ショートが大量に混入し、シグナルとして解釈不能なため。

ティッカーは FINRA CNMS の表記を正とする（クラス株はスラッシュ区切り: "BRK/B"）。
Yahoo Finance 側はハイフン区切りなので `to_yahoo_symbol()` で変換する。
"""

# --- グループ定義（Tier 順）---
US_SEMI_CORE: list[str] = [       # AI/GPU・メモリ・ファウンドリ
    "NVDA", "AMD", "AVGO", "MRVL", "MU", "TSM", "INTC",
]

US_SEMI_EQUIP: list[str] = [      # 製造装置・EUV・検査
    "ASML", "AMAT", "LRCX", "KLAC", "TER", "ONTO",
]

US_SEMI_ADJACENT: list[str] = [   # 周辺・IP・アナログ
    "ARM", "QCOM", "TXN", "ADI", "NXPI", "COHR", "AMKR",
]

US_AI_MEMORY: list[str] = [       # メモリ・HBM（MU は us_semi_core 側に在籍）
    "SNDK",   # サンディスク（2025年にWDCから分離したNAND専業）
    "WDC",    # ウエスタンデジタル（HDD/ニアライン。AIデータセンター需要の受け皿）
    "SKHY",   # SKハイニックス（Nasdaq上場が2026-07-13頃と新しく履歴が浅い）
]

US_AI_INFRA: list[str] = [        # AIインフラ（半導体そのものではないが同じ需要で動く）
    "CRWV",   # コアウィーブ（GPUクラウド）
    "VRT",    # ヴァーティブ（データセンター電源・冷却）
    "SMCI",   # スーパーマイクロ（AIサーバー）
    "ALAB",   # アステララボ（AI接続）
    "CRDO",   # クレド（AI接続・AEC）
    "MPWR",   # モノリシックパワー（AIサーバー電源）
]

US_AI_SOFTWARE: list[str] = ["PLTR"]      # AI関連の高ベータ銘柄（半導体バスケットには入れない）

US_MEGA_CAP: list[str] = [        # マグニフィセント7のうち半導体以外（NVDA は us_semi_core 在籍）
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
]

US_HYPERSCALER_OTHER: list[str] = ["ORCL"]   # AI設備投資の主体。MSFT/GOOGL/AMZN/META は上のグループ

# AIによる代替リスクが語られ、AI半導体ロングの対の空売り側に置かれやすいSaaS。
# 「席課金モデルがAIに侵食される」という筋書きで売られる側。
US_SAAS_AI_RISK: list[str] = [
    "CRM",    # セールスフォース
    "NOW",    # サービスナウ
    "WDAY",   # ワークデイ
    "ADBE",   # アドビ
    "TEAM",   # アトラシアン
    "HUBS",   # ハブスポット
    "DOCU",   # ドキュサイン
    "ZM",     # ズーム
]

# AIの受益側とされるデータ基盤SaaS。半導体と同じ方向に動きやすく、ロング側に置かれやすい。
US_SAAS_AI_INFRA: list[str] = [
    "SNOW",   # スノーフレーク
    "MDB",    # モンゴDB
    "DDOG",   # データドッグ
    "NET",    # クラウドフレア
    "ESTC",   # エラスティック
]

ETF_THEME: list[str] = ["SMH", "SOXX"]    # 半導体テーマETF（ヘッジ需要の代理変数）
ETF_MEMORY: list[str] = ["DRAM"]          # Roundhill Memory ETF（メモリ特化）
ETF_SOFTWARE: list[str] = ["IGV"]         # ソフトウェアETF（SaaS個別との対比用）
ETF_BROAD: list[str] = ["QQQ", "SPY"]     # 広義指数ETF（市場全体のリスクオフ切り分け用）

GROUPS: dict[str, list[str]] = {
    "us_semi_core": US_SEMI_CORE,
    "us_semi_equip": US_SEMI_EQUIP,
    "us_semi_adjacent": US_SEMI_ADJACENT,
    "us_ai_memory": US_AI_MEMORY,
    "us_ai_infra": US_AI_INFRA,
    "us_ai_software": US_AI_SOFTWARE,
    "us_mega_cap": US_MEGA_CAP,
    "us_hyperscaler_other": US_HYPERSCALER_OTHER,
    "us_saas_ai_risk": US_SAAS_AI_RISK,
    "us_saas_ai_infra": US_SAAS_AI_INFRA,
    "etf_theme": ETF_THEME,
    "etf_memory": ETF_MEMORY,
    "etf_software": ETF_SOFTWARE,
    "etf_broad": ETF_BROAD,
}

# --- バスケット定義（US-P2 のバスケット集計で使用）---
# 比率は必ず Σshort_volume / Σtotal_volume のボリューム加重で算出すること。
# 単純平均は小型株の極端値に引きずられるため禁止（QCルール5）。
#
# 各要素はグループ名でもティッカー単体でもよい（グループ名として解決できなければ
# ティッカーとして扱う）。MU のようにグループをまたいで参加させたい銘柄があるため。
BASKETS: dict[str, list[str]] = {
    "SEMI20": ["us_semi_core", "us_semi_equip", "us_semi_adjacent"],
    "SEMI_CORE7": ["us_semi_core"],
    # AI需要で動く銘柄群。アナログ・車載（TXN/ADI/NXPI）や再建途上の INTC は
    # 産業サイクルで動くため意図的に外している（テーマの信号が濁るのを避ける）
    "AI_INFRA": ["us_ai_infra", "NVDA", "AMD", "AVGO", "MRVL", "ARM"],
    # メモリ。DRAM ETF との乖離を見るための対になる構成銘柄
    "MEMORY": ["us_ai_memory", "MU"],
    # AI設備投資を出す側。ここのショートが増えると投資減速懸念が意識されている可能性
    "HYPERSCALER": ["us_hyperscaler_other", "MSFT", "GOOGL", "AMZN", "META", "CRWV"],
    "MAG7": ["us_mega_cap", "NVDA"],
    # AI半導体ロングの対に置かれやすい2群
    "SAAS_AI_RISK": ["us_saas_ai_risk"],
    "SAAS_AI_INFRA": ["us_saas_ai_infra"],
}

# --- ETF乖離を見る組み合わせ（ETF, 対になる構成銘柄バスケット）---
# 対象が噛み合っていない組み合わせ（例: メモリETF vs 半導体20銘柄）は作らない。
DIVERGENCE_PAIRS: list[tuple[str, str]] = [
    ("SMH", "SEMI20"),
    ("SOXX", "SEMI20"),
    ("DRAM", "MEMORY"),
    ("IGV", "SAAS_AI_RISK"),
]

# --- ロング候補 / ショート候補のバスケット対 ---
# ペアトレードで対に置かれやすい組み合わせ。spread = ショート側z20 − ロング側z20 で、
# プラスが大きいほど「ショート側に売りが偏っている」＝その対の取引が入っている候補。
# ⚠️ 空売り比率の水準そのものを引き算しているのではなく、
#    それぞれが自分の過去分布からどれだけ離れたか（Zスコア）を比べている。
BASKET_PAIRS: list[dict] = [
    {
        "name": "AI半導体 vs AI代替リスクSaaS",
        "long": "AI_INFRA",
        "short": "SAAS_AI_RISK",
        "note": "AI設備投資の受益側を買い、AIに置き換えられる懸念のあるSaaSを売る対",
    },
    {
        "name": "AI半導体 vs データ基盤SaaS",
        "long": "AI_INFRA",
        "short": "SAAS_AI_INFRA",
        "note": "同じAI需要で動くとされる2群。差が開くとどちらかに偏りが出ている",
    },
    {
        "name": "メモリ vs 半導体全体",
        "long": "SEMI20",
        "short": "MEMORY",
        "note": "メモリ固有の需給か、半導体全体の動きかを切り分ける",
    },
    {
        "name": "ハイパースケーラー vs AI半導体",
        "long": "AI_INFRA",
        "short": "HYPERSCALER",
        "note": "設備投資を出す側と受け取る側。出す側だけ売られると投資減速懸念の可能性",
    },
]

# 銘柄→所属グループ（レポートの並び順にも使う）
TICKER_GROUP: dict[str, str] = {
    ticker: group
    for group, tickers in GROUPS.items()
    for ticker in tickers
}

# 監視対象の全ティッカー（重複排除・定義順を保持）
US_UNIVERSE: list[str] = list(TICKER_GROUP.keys())


def basket_members(basket_name: str) -> list[str]:
    """バスケット名から構成銘柄リストを返す。未定義なら空リスト。

    定義の各要素はグループ名でもティッカー単体でもよい。重複は除いて定義順を保つ。
    """
    entries = BASKETS.get(basket_name)
    if not entries:
        return []

    members: list[str] = []
    for entry in entries:
        members.extend(GROUPS.get(entry, [entry]))

    seen: set[str] = set()
    unique: list[str] = []
    for ticker in members:
        if ticker not in seen:
            seen.add(ticker)
            unique.append(ticker)
    return unique


def to_yahoo_symbol(ticker: str) -> str:
    """FINRA 表記のティッカーを Yahoo Finance 表記へ変換する（BRK/B → BRK-B）。"""
    return ticker.replace("/", "-")
