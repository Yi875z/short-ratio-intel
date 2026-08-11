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

    calculated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<MarketShortRatioDaily "
            f"date={self.date} "
            f"ratio={self.short_ratio_pct:.1f}%>"
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
