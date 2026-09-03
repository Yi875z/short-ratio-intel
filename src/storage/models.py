"""
SQLAlchemy モデル定義
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ShortRatioDaily(Base):
    """業種別空売り比率 日次データ"""

    __tablename__ = "short_ratio_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)       # YYYY-MM-DD
    s33_code = Column(String(10), nullable=False, index=True)   # 業種コード
    sector_name = Column(String(50), nullable=False)             # 業種名

    sell_ex_short_va = Column(Float, nullable=False, default=0)  # 実注文売買代金
    shrt_with_res_va = Column(Float, nullable=False, default=0)  # 価格規制有り空売り
    shrt_no_res_va = Column(Float, nullable=False, default=0)    # 価格規制無し空売り
    total_short_va = Column(Float, nullable=False, default=0)    # 空売り合計
    total_volume_va = Column(Float, nullable=False, default=0)   # 売買代金合計

    short_ratio_pct = Column(Float, nullable=False, default=0)   # 空売り比率(%)

    # JPX内訳の出所。'jpx_pdf'=内訳4列は実測値 / 'scraper'=内訳を持たず0。
    # 内訳4列が nullable=False, default=0 のため「未取得」と「本当に0」を
    # 値では区別できない。推測をやめてここに事実を記録する。
    breakdown_source = Column(String(16), nullable=True)

    calculated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("date", "s33_code", name="uq_date_sector"),
    )

    def __repr__(self) -> str:
        return (
            f"<ShortRatioDaily "
            f"date={self.date} "
            f"sector={self.sector_name} "
            f"ratio={self.short_ratio_pct:.1f}%>"
        )


class MarketShortRatioDaily(Base):
    """東証全体の空売り比率 日次データ"""

    __tablename__ = "market_short_ratio_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True, index=True)

    sell_ex_short_va = Column(Float, nullable=False, default=0)  # 実注文売買代金
    shrt_with_res_va = Column(Float, nullable=False, default=0)  # 価格規制有り空売り
    shrt_no_res_va = Column(Float, nullable=False, default=0)    # 価格規制無し空売り
    total_short_va = Column(Float, nullable=False, default=0)    # 空売り合計
    total_volume_va = Column(Float, nullable=False, default=0)   # 売買代金合計

    short_ratio_pct = Column(Float, nullable=False, default=0)
    dod_change = Column(Float, nullable=True)

    # JPX内訳の出所。'jpx_pdf'=内訳4列は実測値 / 'scraper'=内訳を持たず0。
    # 内訳4列が nullable=False, default=0 のため「未取得」と「本当に0」を
    # 値では区別できない。推測をやめてここに事実を記録する。
    breakdown_source = Column(String(16), nullable=True)

    calculated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<MarketShortRatioDaily "
            f"date={self.date} "
            f"ratio={self.short_ratio_pct:.1f}%>"
        )


class MarketBreadthDaily(Base):
    """市場の広がり（騰落銘柄数）と指数の値動き 日次データ

    出所は J-Quants API v2（Light 以上）。全銘柄日足と上場銘柄一覧から自前で数える。

    ⚠️ 空売り集計（short_ratio_daily / market_short_ratio_daily）とは**対象範囲が違う**。
       あちらは東証全体（ETF・REIT 込み）、こちらは market_scope 列の市場区分ごと。
       両者を跨いだ加減乗除・比率化は行わないこと。
    ⚠️ 騰落判定は調整後終値どうしの比較（分割・併合をまたいでも壊れないため）。
       前日または当日の足が無い銘柄は not_compared に積み、補間しない。
    """

    __tablename__ = "market_breadth_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)          # YYYY-MM-DD
    market_scope = Column(String(20), nullable=False, index=True)  # TSE_PRIME 等
    scope_label = Column(String(50), nullable=False, default="")   # 画面表示用の日本語名

    # raw列（数え上げた生の件数）
    advancing_issues = Column(Integer, nullable=True)
    declining_issues = Column(Integer, nullable=True)
    unchanged_issues = Column(Integer, nullable=True)
    not_compared_issues = Column(Integer, nullable=True)   # 判定できなかった銘柄数
    universe_issues = Column(Integer, nullable=True)       # 対象日時点の母集団

    # 指数の値動き。scope に依らず市場共通の文脈なので同じ行に持たせる。
    topix_close = Column(Float, nullable=True)
    topix_prev_close = Column(Float, nullable=True)
    topix_change_pct = Column(Float, nullable=True)

    source = Column(String(32), nullable=False, default="JQUANTS_V2")
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "market_scope", name="uq_breadth_date_scope"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketBreadthDaily date={self.date} scope={self.market_scope} "
            f"adv={self.advancing_issues} dec={self.declining_issues}>"
        )


class SectorFlowFeatureDaily(Base):
    """業種別フロー特徴量 日次データ（Phase 0: 保存のみ・判定なし）

    「大量の空売りフローが出た業種で、市場はその売りをどう処理したのか」を
    後から検証できるようにするための特徴量と、その将来リターンを同じ行に持つ。

    ⚠️ この行は**状態分類を持たない**。既存システムは既に1日63件の判定を出しているが、
       そのどれも翌日の値動きと突き合わせて検証されていない。判定を増やす前に、
       特徴量と将来リターンを並べて測れる状態を作るのがこのテーブルの目的。
    ⚠️ 母集団が空売り集計と違う。あちらは東証全体（外国株券等を含む）、
       こちらは scope 列のとおり普通株の主要3市場。**両者を掛け合わせない**
       （join して並べるのは可、割り算は不可）。
    ⚠️ 将来リターンは検証専用。当日の判定に使うと未来の情報を使うことになる。
    """

    __tablename__ = "sector_flow_features_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)       # YYYY-MM-DD
    s33_code = Column(String(10), nullable=False, index=True)   # 33業種コード
    scope = Column(String(40), nullable=False, default="")      # 母集団の定義

    # 母集団（欠損を補間していないことを監査できるよう両方持つ）
    constituents = Column(Integer, nullable=True)
    compared = Column(Integer, nullable=True)

    # 値動き
    ret_cap_weighted = Column(Float, nullable=True)      # 前日時価総額加重の騰落率(%)
    ret_equal_weighted = Column(Float, nullable=True)    # 単純平均の騰落率(%)
    excess_ret_vs_topix = Column(Float, nullable=True)   # 対TOPIX相対(pt)

    # 市場がその売りをどう処理したか
    above_vwap_pct = Column(Float, nullable=True)        # 終値>当日VWAP の銘柄比率(%)
    high_close_pct = Column(Float, nullable=True)        # 終値位置>=0.75 の比率(%)
    advancing_pct = Column(Float, nullable=True)         # 前日比プラスの比率(%)
    close_above_open_pct = Column(Float, nullable=True)  # 終値>始値の比率(%)
    close_location_median = Column(Float, nullable=True)

    # 売買代金と上位バスケット
    turnover_total = Column(Float, nullable=True)        # 円
    top_n = Column(Integer, nullable=True)
    top_n_turnover_share = Column(Float, nullable=True)  # (%)
    top_n_above_vwap = Column(Integer, nullable=True)
    top_n_high_close = Column(Integer, nullable=True)
    top_n_advancing = Column(Integer, nullable=True)
    top_n_codes = Column(String, nullable=True)          # 監査用のJSON配列

    # 将来リターン（別パスで後から埋める。判定には使わない）
    fwd_ret_1d = Column(Float, nullable=True)
    fwd_ret_3d = Column(Float, nullable=True)
    fwd_ret_5d = Column(Float, nullable=True)
    fwd_excess_1d = Column(Float, nullable=True)
    fwd_excess_3d = Column(Float, nullable=True)
    fwd_excess_5d = Column(Float, nullable=True)

    source = Column(String(32), nullable=False, default="JQUANTS_V2")
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "s33_code", name="uq_sector_feature_date_s33"),
    )

    def __repr__(self) -> str:
        return (
            f"<SectorFlowFeatureDaily date={self.date} s33={self.s33_code} "
            f"ret={self.ret_cap_weighted} vwap%={self.above_vwap_pct}>"
        )


class AiReport(Base):
    """AIが生成した日次レポート"""

    __tablename__ = "ai_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True, index=True)
    macro_context = Column(String(2000), nullable=False)
    report_markdown = Column(String, nullable=False)        # フルレポート本文
    report_json = Column(String, nullable=True)             # 構造化JSONキャッシュ
    model_used = Column(String(100), nullable=True)
    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<AiReport date={self.date}>"


class MarketThemeSnapshot(Base):
    """日次の市場テーマ判定スナップショット"""

    __tablename__ = "market_theme_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)
    theme_key = Column(String(100), nullable=False)
    theme_name = Column(String(200), nullable=False)
    score = Column(Float, nullable=False, default=0)
    status = Column(String(50), nullable=False, default="")
    confidence = Column(String(50), nullable=False, default="")
    evidence_json = Column(String, nullable=False, default="[]")
    related_sectors_json = Column(String, nullable=False, default="[]")
    unverified_data_json = Column(String, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "theme_key", name="uq_date_theme_key"),
    )

    def __repr__(self) -> str:
        return f"<MarketThemeSnapshot date={self.date} theme={self.theme_name}>"


class MarketNewsSnapshot(Base):
    """日次の市場ニュース検索結果スナップショット"""

    __tablename__ = "market_news_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)
    query = Column(String(300), nullable=False, default="")
    title = Column(String(500), nullable=False, default="")
    url = Column(String(1000), nullable=False, default="")
    source = Column(String(200), nullable=False, default="")
    published_date = Column(String(50), nullable=False, default="")
    snippet = Column(String, nullable=False, default="")
    score = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "url", "title", name="uq_date_news_item"),
    )

    def __repr__(self) -> str:
        return f"<MarketNewsSnapshot date={self.date} title={self.title[:30]}>"


class KnowledgeDocument(Base):
    """外部ナレッジ（思考データ）の本文。

    公開リポジトリにファイルを置かずに済むよう、本文を Supabase に保存する。
    Streamlit Cloud / GitHub Actions はここから読み、IP を非公開のまま使う。
    """

    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), nullable=False, unique=True, index=True)  # 例: global_macro
    filename = Column(String(200), nullable=False, default="")
    content = Column(String, nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<KnowledgeDocument key={self.key} len={len(self.content)}>"


# ==================================================================
# 米国ショートフロー（US-P1）
#
# 日本側テーブル（業種別・売買代金JPYベース）とは粒度も単位も異なるため、
# 意図的に別テーブルとして分離している。両者を跨いだ加減乗除は行わない。
# ==================================================================

class UsShortVolumeDaily(Base):
    """米国個別銘柄の日次ショートボリューム（FINRA CNMS 等）

    ⚠️ これは「フロー」であって空売り残高ではない。残高は UsShortInterest（US-P3）で扱う。
    ⚠️ short_ratio_pct は必ず同一ソース内の short_volume / reported_total_volume で算出する。
       consolidated volume（UsMarketDaily.market_volume）を分母にしてはならない。
    """

    __tablename__ = "us_short_volume_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)        # YYYY-MM-DD
    ticker = Column(String(16), nullable=False, index=True)      # FINRA表記（BRK/B 等）
    region = Column(String(4), nullable=False, default="US")
    source = Column(String(32), nullable=False)                  # FINRA_CNMS
    venue_scope = Column(String(16), nullable=False)             # OFF_EXCHANGE

    # raw列（取得生値）: FINRA は小数を含むため Float で受ける
    short_volume = Column(Float, nullable=True)
    short_exempt_volume = Column(Float, nullable=True)
    reported_total_volume = Column(Float, nullable=True)         # ★このソースの報告分出来高

    # calculated列（計算値）
    short_ratio_pct = Column(Float, nullable=True)

    market_codes = Column(String(32), nullable=True)             # "B,Q,N" 等
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "ticker", "source", name="uq_us_svd_date_ticker_source"),
    )

    def __repr__(self) -> str:
        ratio = f"{self.short_ratio_pct:.1f}%" if self.short_ratio_pct is not None else "N/A"
        return f"<UsShortVolumeDaily date={self.date} ticker={self.ticker} ratio={ratio}>"


class UsMarketDaily(Base):
    """米国個別銘柄の日次OHLCV（Yahoo Finance chart API）

    ⚠️ market_volume は consolidated volume（市場全体の出来高）。
       ショート比率の分母に使ってはならない。用途は騰落率・終値位置・出来高比のみ。
    """

    __tablename__ = "us_market_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)
    ticker = Column(String(16), nullable=False, index=True)

    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    adj_close = Column(Float, nullable=True)
    market_volume = Column(Float, nullable=True)

    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "ticker", name="uq_us_market_date_ticker"),
    )

    def __repr__(self) -> str:
        close = f"{self.close:.2f}" if self.close is not None else "N/A"
        return f"<UsMarketDaily date={self.date} ticker={self.ticker} close={close}>"


class UsShortInterest(Base):
    """米国の空売り残高（隔週。FINRA Consolidated Short Interest）

    ⚠️ 日次のフロー（UsShortVolumeDaily）とは別概念。混ぜて計算してはならない。
       こちらは基準日時点で未決済のまま残っている空売りの株数。
    ⚠️ 公表は基準日から2週間前後遅れる。利用側には必ず基準日と経過日数を示すこと。
    """

    __tablename__ = "us_short_interest"

    id = Column(Integer, primary_key=True, autoincrement=True)
    settlement_date = Column(String(10), nullable=False, index=True)   # 基準日 YYYY-MM-DD
    ticker = Column(String(16), nullable=False, index=True)
    issue_name = Column(String(120), nullable=True)

    current_short_position = Column(Float, nullable=True)    # 今回の残高（株数）
    previous_short_position = Column(Float, nullable=True)   # 前回の残高
    average_daily_volume = Column(Float, nullable=True)      # 平均日次出来高
    days_to_cover = Column(Float, nullable=True)             # 買い戻しに要する日数
    change_percent = Column(Float, nullable=True)            # 前回比(%)

    source = Column(String(32), nullable=False)
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("settlement_date", "ticker", "source", name="uq_us_si_date_ticker_source"),
    )

    def __repr__(self) -> str:
        return f"<UsShortInterest {self.settlement_date} {self.ticker} {self.current_short_position}>"
