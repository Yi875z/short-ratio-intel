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

ETF_THEME: list[str] = ["SMH", "SOXX"]    # テーマETF（ヘッジ需要の代理変数）
ETF_BROAD: list[str] = ["QQQ", "SPY"]     # 広義指数ETF（市場全体のリスクオフ切り分け用）

GROUPS: dict[str, list[str]] = {
    "us_semi_core": US_SEMI_CORE,
    "us_semi_equip": US_SEMI_EQUIP,
    "us_semi_adjacent": US_SEMI_ADJACENT,
    "etf_theme": ETF_THEME,
    "etf_broad": ETF_BROAD,
}

# --- バスケット定義（US-P2 のバスケット集計で使用）---
# 比率は必ず Σshort_volume / Σtotal_volume のボリューム加重で算出すること。
# 単純平均は小型株の極端値に引きずられるため禁止（QCルール5）。
BASKETS: dict[str, list[str]] = {
    "SEMI20": ["us_semi_core", "us_semi_equip", "us_semi_adjacent"],
    "SEMI_CORE7": ["us_semi_core"],
}

# 銘柄→所属グループ（レポートの並び順にも使う）
TICKER_GROUP: dict[str, str] = {
    ticker: group
    for group, tickers in GROUPS.items()
    for ticker in tickers
}

# 監視対象の全ティッカー（重複排除・定義順を保持）
US_UNIVERSE: list[str] = list(TICKER_GROUP.keys())


def basket_members(basket_name: str) -> list[str]:
    """バスケット名から構成銘柄リストを返す。未定義なら空リスト。"""
    groups = BASKETS.get(basket_name)
    if not groups:
        return []
    members: list[str] = []
    for group in groups:
        members.extend(GROUPS.get(group, []))
    return members


def to_yahoo_symbol(ticker: str) -> str:
    """FINRA 表記のティッカーを Yahoo Finance 表記へ変換する（BRK/B → BRK-B）。"""
    return ticker.replace("/", "-")
