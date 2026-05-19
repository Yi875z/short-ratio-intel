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
