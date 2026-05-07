"""
SQLite データベース接続・CRUD操作
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import create_engine, select, desc, delete
from sqlalchemy.orm import Session

from config.settings import DB_PATH, REPORTS_DIR
from src.storage.models import Base, ShortRatioDaily, MarketShortRatioDaily, AiReport


# ------------------------------------------------------------------
# エンジン初期化
# ------------------------------------------------------------------

def get_engine():
    """SQLAlchemy エンジンを返す（DB自動作成）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{DB_PATH}"
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    return engine


_engine = None


def get_db_engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
        logger.info(f"DB接続: {DB_PATH}")
    return _engine


# ------------------------------------------------------------------
# 空売り比率データ
# ------------------------------------------------------------------

def upsert_short_ratio_records(records: list[dict]) -> int:
    """
    空売り比率レコードをUPSERT（既存なら更新、なければ挿入）。
    Returns: 保存件数
    """
    if not records:
        return 0

    records = _filter_valid_short_ratio_records(records)
    if not records:
        return 0

    engine = get_db_engine()
    saved = 0

    with Session(engine) as session:
        for r in records:
            # 既存チェック
            sell_ex_short_va = r.get("SellExShortVa", 0)
            shrt_with_res_va = r.get("ShrtWithResVa", 0)
            shrt_no_res_va = r.get("ShrtNoResVa", 0)
            total_short_va = r.get("TotalShortVa", 0)
            total_volume_va = r.get("TotalVolumeVa", 0)

            stmt = select(ShortRatioDaily).where(
                ShortRatioDaily.date == r["Date"],
                ShortRatioDaily.s33_code == r["S33"],
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                # 更新
                existing.short_ratio_pct = r["ShortRatioPct"]
                existing.sell_ex_short_va = sell_ex_short_va
                existing.shrt_with_res_va = shrt_with_res_va
                existing.shrt_no_res_va = shrt_no_res_va
                existing.total_short_va = total_short_va
                existing.total_volume_va = total_volume_va
                existing.calculated_at = datetime.utcnow()
            else:
                # 新規挿入
                row = ShortRatioDaily(
                    date=r["Date"],
                    s33_code=r["S33"],
                    sector_name=r["SectorName"],
                    sell_ex_short_va=sell_ex_short_va,
                    shrt_with_res_va=shrt_with_res_va,
                    shrt_no_res_va=shrt_no_res_va,
                    total_short_va=total_short_va,
                    total_volume_va=total_volume_va,
                    short_ratio_pct=r["ShortRatioPct"],
                )
                session.add(row)
            saved += 1

        session.commit()

    logger.info(f"{saved}件を保存しました")
    return saved


def get_short_ratio_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    s33_code: Optional[str] = None,
) -> pd.DataFrame:
    """
    条件指定でデータを取得し DataFrame で返す。
    """
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(ShortRatioDaily).order_by(
            ShortRatioDaily.date, ShortRatioDaily.s33_code
        )

        if date:
            stmt = stmt.where(ShortRatioDaily.date == date)
        if from_date:
            stmt = stmt.where(ShortRatioDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(ShortRatioDaily.date <= to_date)
        if s33_code:
            stmt = stmt.where(ShortRatioDaily.s33_code == s33_code)

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "date": r.date,
        "s33_code": r.s33_code,
        "sector_name": r.sector_name,
        "sell_ex_short_va": r.sell_ex_short_va,
        "shrt_with_res_va": r.shrt_with_res_va,
        "shrt_no_res_va": r.shrt_no_res_va,
        "total_short_va": r.total_short_va,
        "short_ratio_pct": r.short_ratio_pct,
        "total_volume_va": r.total_volume_va,
    } for r in rows])


def get_latest_date() -> Optional[str]:
    """DBに保存されている最新日付を返す"""
    engine = get_db_engine()
    with Session(engine) as session:
        stmt = select(ShortRatioDaily.date).order_by(
            desc(ShortRatioDaily.date)
        ).limit(1)
        result = session.execute(stmt).scalar_one_or_none()
    return result


def get_saved_short_ratio_dates() -> list[str]:
    """保存済み空売り比率データの日付一覧を新しい順で返す"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(ShortRatioDaily.date)
            .distinct()
            .order_by(desc(ShortRatioDaily.date))
        ).scalars().all()
    return list(rows)


def delete_short_ratio_records_for_dates(dates: list[str]) -> int:
    """指定日付の業種別空売り比率データを削除する。"""
    if not dates:
        return 0

    engine = get_db_engine()
    with Session(engine) as session:
        result = session.execute(
            delete(ShortRatioDaily).where(ShortRatioDaily.date.in_(dates))
        )
        session.commit()
        deleted = result.rowcount or 0

    logger.info(f"業種別データ {deleted}件を削除しました: {dates}")
    return deleted


# ------------------------------------------------------------------
# 東証全体 空売り比率データ
# ------------------------------------------------------------------

def upsert_market_short_ratio_records(records: list[dict]) -> int:
    """東証全体の空売り比率レコードをUPSERTする。"""
    if not records:
        return 0

    records = _filter_valid_market_short_ratio_records(records)
    if not records:
        return 0

    engine = get_db_engine()
    saved = 0

    with Session(engine) as session:
        for r in records:
            date_value = r["Date"]
            shrt_with_res_va = r.get("ShrtWithResVa", 0)
            shrt_no_res_va = r.get("ShrtNoResVa", 0)
            total_short_va = r.get("TotalShortVa", shrt_with_res_va + shrt_no_res_va)

            existing = session.execute(
                select(MarketShortRatioDaily).where(
                    MarketShortRatioDaily.date == date_value
                )
            ).scalar_one_or_none()

            if existing:
                existing.short_ratio_pct = r["ShortRatioPct"]
                if r.get("DodChange") is not None:
                    existing.dod_change = r.get("DodChange")
                existing.sell_ex_short_va = r.get("SellExShortVa", 0)
                existing.shrt_with_res_va = shrt_with_res_va
                existing.shrt_no_res_va = shrt_no_res_va
                existing.total_short_va = total_short_va
                existing.total_volume_va = r.get("TotalVolumeVa", 0)
                existing.calculated_at = datetime.utcnow()
            else:
                session.add(MarketShortRatioDaily(
                    date=date_value,
                    short_ratio_pct=r["ShortRatioPct"],
                    dod_change=r.get("DodChange"),
                    sell_ex_short_va=r.get("SellExShortVa", 0),
                    shrt_with_res_va=shrt_with_res_va,
                    shrt_no_res_va=shrt_no_res_va,
                    total_short_va=total_short_va,
                    total_volume_va=r.get("TotalVolumeVa", 0),
                ))
            saved += 1

        session.commit()

    logger.info(f"東証全体データ {saved}件を保存しました")
    return saved


def _filter_valid_short_ratio_records(records: list[dict]) -> list[dict]:
    """日付単位で業種別空売り比率の範囲外データを破棄する。"""
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.get("Date", ""), []).append(record)

    valid_records = []
    for date_value, day_records in grouped.items():
        invalid = [
            r for r in day_records
            if not _is_valid_ratio(r.get("ShortRatioPct"))
        ]
        if invalid:
            logger.error(
                "業種別空売り比率の保存を日付単位で破棄します: "
                f"date={date_value}, invalid_count={len(invalid)}, "
                f"examples={[(r.get('SectorName'), r.get('ShortRatioPct')) for r in invalid[:5]]}"
            )
            continue
        valid_records.extend(day_records)
    return valid_records


def _filter_valid_market_short_ratio_records(records: list[dict]) -> list[dict]:
    """東証全体空売り比率の範囲外データを破棄する。"""
    valid_records = []
    for record in records:
        if _is_valid_ratio(record.get("ShortRatioPct")):
            valid_records.append(record)
        else:
            logger.error(
                "東証全体空売り比率の保存を破棄します: "
                f"date={record.get('Date')}, ratio={record.get('ShortRatioPct')}"
            )
    return valid_records


def _is_valid_ratio(value) -> bool:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 <= ratio <= 100.0


def get_market_short_ratio_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> pd.DataFrame:
    """東証全体の空売り比率データをDataFrameで返す。"""
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(MarketShortRatioDaily).order_by(MarketShortRatioDaily.date)

        if date:
            stmt = stmt.where(MarketShortRatioDaily.date == date)
        if from_date:
            stmt = stmt.where(MarketShortRatioDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(MarketShortRatioDaily.date <= to_date)

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "date": r.date,
        "short_ratio_pct": r.short_ratio_pct,
        "dod_change": r.dod_change,
        "sell_ex_short_va": r.sell_ex_short_va,
        "shrt_with_res_va": r.shrt_with_res_va,
        "shrt_no_res_va": r.shrt_no_res_va,
        "total_short_va": r.total_short_va,
        "total_volume_va": r.total_volume_va,
    } for r in rows])


# ------------------------------------------------------------------
# AIレポート
# ------------------------------------------------------------------

def save_ai_report(date: str, macro_context: str,
                   report_markdown: str, report_json: str = "",
                   model_used: str = "") -> None:
    """AIレポートをDBとMarkdownファイルに保存"""
    engine = get_db_engine()
    with Session(engine) as session:
        existing = session.execute(
            select(AiReport).where(AiReport.date == date)
        ).scalar_one_or_none()

        if existing:
            existing.report_markdown = report_markdown
            existing.report_json = report_json
            existing.generated_at = datetime.utcnow()
        else:
            session.add(AiReport(
                date=date,
                macro_context=macro_context,
                report_markdown=report_markdown,
                report_json=report_json,
                model_used=model_used,
            ))
        session.commit()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"short_ratio_report_{date}.md"
    report_path.write_text(report_markdown, encoding="utf-8")

    logger.info(f"AIレポート保存完了: {date}")


def get_ai_report(date: str) -> Optional[AiReport]:
    """指定日のAIレポートを取得"""
    engine = get_db_engine()
    with Session(engine) as session:
        return session.execute(
            select(AiReport).where(AiReport.date == date)
        ).scalar_one_or_none()


def get_ai_report_dates() -> list[str]:
    """保存済みAIレポートの日付一覧を新しい順で返す"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(AiReport.date).order_by(desc(AiReport.date))
        ).scalars().all()
    return list(rows)
