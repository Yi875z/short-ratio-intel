"""
データベース接続・CRUD操作

接続先は環境変数 DATABASE_URL で切り替わる:
- DATABASE_URL 未設定 → ローカル SQLite (従来どおり / 開発用)
- DATABASE_URL 設定   → Supabase(PostgreSQL) (GitHub Actions / Streamlit Cloud 用)
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import create_engine, select, desc, delete
from sqlalchemy.orm import Session

from config.settings import DB_PATH, REPORTS_DIR
from src.storage.models import (
    AiReport,
    Base,
    KnowledgeDocument,
    MarketBreadthDaily,
    MarketNewsSnapshot,
    MarketShortRatioDaily,
    MarketThemeSnapshot,
    SectorFlowFeatureDaily,
    ShortRatioDaily,
    UsMarketDaily,
    UsShortInterest,
    UsShortVolumeDaily,
)


# ------------------------------------------------------------------
# エンジン初期化
# ------------------------------------------------------------------

def get_engine():
    """SQLAlchemy エンジンを返す（DB自動作成）。

    環境変数 DATABASE_URL があれば Supabase(PostgreSQL) に、
    なければ従来どおりローカル SQLite に接続する。
    """
    database_url = os.environ.get("DATABASE_URL") or ""
    if database_url:
        # SQLAlchemy は postgresql:// を要求する。古い postgres:// 形式を正規化
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        # pool_pre_ping: Supabase pooler の切断済みコネクションを自動検知して張り直す
        engine = create_engine(database_url, echo=False, pool_pre_ping=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
    Base.metadata.create_all(engine)
    return engine


_engine = None


def get_db_engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
        backend = "PostgreSQL(Supabase)" if os.environ.get("DATABASE_URL") else f"SQLite({DB_PATH})"
        logger.info(f"DB接続: {backend}")
    return _engine


# ------------------------------------------------------------------
# 空売り比率データ
# ------------------------------------------------------------------

BREAKDOWN_SOURCE_JPX = "jpx_pdf"
BREAKDOWN_SOURCE_SCRAPER = "scraper"


def _breakdown_source(record: dict) -> str:
    return BREAKDOWN_SOURCE_JPX if _record_has_breakdown(record) else BREAKDOWN_SOURCE_SCRAPER


def _record_has_breakdown(record: dict) -> bool:
    """取得結果がJPX内訳を持っているか。

    stock-marketdata のスクレイパーは比率と売買代金しか持たず、内訳を 0 で返す。
    「内訳が無い取得結果」と「空売りが無かった日」は別の事実なので、
    0 の結果で既存の内訳を上書きしてはいけない。市場全体・業種別の両方で
    同じ判定を使う（判定が2箇所に散ると次は片方だけ直す事故になる）。
    """
    return any(
        record.get(key) or 0
        for key in ("TotalShortVa", "ShrtWithResVa", "ShrtNoResVa")
    )


def dates_with_breakdown(records: list[dict]) -> list[str]:
    """内訳を持つ取得結果の日付だけを返す。

    業種別は DELETE→INSERT で入れ替えるため、内訳なしの結果でこれをやると
    既存の内訳ごと消える。削除を許すのはこの関数が返した日だけにすること。
    """
    return sorted({
        record["Date"] for record in records
        if record.get("Date") and _record_has_breakdown(record)
    })


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
                existing.total_volume_va = total_volume_va
                existing.calculated_at = datetime.utcnow()

                # ⚠️ 市場全体と同じ理由で、内訳なしの結果で既存の内訳を潰さない。
                if _record_has_breakdown(r):
                    existing.sell_ex_short_va = sell_ex_short_va
                    existing.shrt_with_res_va = shrt_with_res_va
                    existing.shrt_no_res_va = shrt_no_res_va
                    existing.total_short_va = total_short_va
                    existing.breakdown_source = BREAKDOWN_SOURCE_JPX
                elif existing.total_short_va:
                    logger.info(
                        f"{r['Date']} {r['S33']}: 内訳なしの取得結果のため既存の内訳を保持します"
                    )
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
                    breakdown_source=_breakdown_source(r),
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
                existing.total_volume_va = r.get("TotalVolumeVa", 0)
                existing.calculated_at = datetime.utcnow()

                # ⚠️ 内訳を持たないレコードで、既に入っている内訳を0で潰さないこと。
                # stock-marketdata のスクレイパーは比率と売買代金しか持たず内訳を0で返す。
                # JPX公式PDFが取れなかっただけの日に上書きすると、取得済みの内訳が失われる。
                # 実測（2026-09-03）:
                #   - 一覧ページは当月ぶんのみを載せる。前月以前の日付は一覧から引けない。
                #   - 0 で潰れていた22営業日は5つの塊で、すべてその月の最終営業日で終わっていた
                #     （4/30・5/29・6/30・7/31・8/31）。
                # 推定（実行ログが無いため未確定）: 月が替わったあとの日次実行が直近N営業日を
                #   取りに行き、前月末ぶんは一覧に無いためスクレイパーへ落ち、0 で上書きした。
                if _record_has_breakdown(r):
                    existing.sell_ex_short_va = r.get("SellExShortVa", 0)
                    existing.shrt_with_res_va = shrt_with_res_va
                    existing.shrt_no_res_va = shrt_no_res_va
                    existing.total_short_va = total_short_va
                    existing.breakdown_source = BREAKDOWN_SOURCE_JPX
                elif existing.total_short_va:
                    logger.info(
                        f"{date_value}: 内訳なしの取得結果のため、既存のJPX内訳を保持します"
                    )
            else:
                session.add(MarketShortRatioDaily(
                    date=date_value,
                    short_ratio_pct=r["ShortRatioPct"],
                    dod_change=r.get("DodChange"),
                    breakdown_source=_breakdown_source(r),
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
        "breakdown_source": r.breakdown_source,
    } for r in rows])


# ------------------------------------------------------------------
# 市場の広がり（騰落銘柄数・指数の値動き）
#
# 出所は J-Quants API v2。空売り集計とは対象範囲が異なるため別テーブルにしてある。
# 両者を跨いだ比率化は行わない（market_scope 列がその境界を明示する）。
# ------------------------------------------------------------------

def upsert_market_breadth_records(records: list[dict]) -> int:
    """騰落銘柄数レコードをUPSERTする。

    レコードは market_breadth.BreadthCounts.to_dict() 形式に
    TOPIX の値を足した dict。欠落フィールドは None のまま保存する
    （前日値のコピーや補間は行わない）。
    同一 (date, market_scope) の再投入で行数は増えない（冪等）。
    """
    if not records:
        return 0

    now = datetime.utcnow()
    rows: list[dict] = []
    for r in records:
        date_value = r.get("date")
        scope = r.get("scope") or r.get("market_scope")
        if not date_value or not scope:
            logger.warning(f"騰落銘柄数の必須項目が欠落: {r}")
            continue
        rows.append({
            "date": date_value,
            "market_scope": scope,
            "scope_label": r.get("scope_label") or "",
            "advancing_issues": r.get("advancing"),
            "declining_issues": r.get("declining"),
            "unchanged_issues": r.get("unchanged"),
            "not_compared_issues": r.get("not_compared"),
            "universe_issues": r.get("universe"),
            "topix_close": r.get("topix_close"),
            "topix_prev_close": r.get("topix_prev_close"),
            "topix_change_pct": r.get("topix_change_pct"),
            "source": r.get("source") or "JQUANTS_V2",
            "ingested_at": now,
        })

    if not rows:
        return 0

    engine = get_db_engine()
    with Session(engine) as session:
        saved = _apply_bulk_upsert(
            session, MarketBreadthDaily, rows, ("date", "market_scope")
        )
        session.commit()

    logger.info(f"騰落銘柄数 {saved}件を保存しました")
    return saved


def get_market_breadth_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    market_scope: Optional[str] = None,
) -> pd.DataFrame:
    """条件指定で騰落銘柄数を DataFrame で返す。"""
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(MarketBreadthDaily).order_by(
            MarketBreadthDaily.date, MarketBreadthDaily.market_scope
        )
        if date:
            stmt = stmt.where(MarketBreadthDaily.date == date)
        if from_date:
            stmt = stmt.where(MarketBreadthDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(MarketBreadthDaily.date <= to_date)
        if market_scope:
            stmt = stmt.where(MarketBreadthDaily.market_scope == market_scope)

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "date": r.date,
        "market_scope": r.market_scope,
        "scope_label": r.scope_label,
        "advancing_issues": r.advancing_issues,
        "declining_issues": r.declining_issues,
        "unchanged_issues": r.unchanged_issues,
        "not_compared_issues": r.not_compared_issues,
        "universe_issues": r.universe_issues,
        "topix_close": r.topix_close,
        "topix_prev_close": r.topix_prev_close,
        "topix_change_pct": r.topix_change_pct,
    } for r in rows])


def get_saved_market_breadth_dates(market_scope: Optional[str] = None) -> list[str]:
    """保存済み騰落銘柄数の日付一覧を新しい順で返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        stmt = select(MarketBreadthDaily.date).distinct()
        if market_scope:
            stmt = stmt.where(MarketBreadthDaily.market_scope == market_scope)
        rows = session.execute(
            stmt.order_by(desc(MarketBreadthDaily.date))
        ).scalars().all()
    return list(rows)


def get_market_breadth_latest_date(market_scope: Optional[str] = None) -> Optional[str]:
    """保存済み騰落銘柄数の最新日付を返す。"""
    dates = get_saved_market_breadth_dates(market_scope)
    return dates[0] if dates else None


# ------------------------------------------------------------------
# 業種別フロー特徴量（Phase 0: 保存のみ・判定なし）
#
# 空売り集計とは母集団が違うため別テーブル。join して並べるのは可、
# 割り算して比率を作るのは不可（scope 列がその境界）。
# ------------------------------------------------------------------

_FEATURE_COLUMNS = (
    "constituents", "compared",
    "ret_cap_weighted", "ret_equal_weighted", "excess_ret_vs_topix",
    "above_vwap_pct", "high_close_pct", "advancing_pct",
    "close_above_open_pct", "close_location_median",
    "turnover_total", "top_n", "top_n_turnover_share",
    "top_n_above_vwap", "top_n_high_close", "top_n_advancing",
)

_FORWARD_COLUMNS = (
    "fwd_ret_1d", "fwd_ret_3d", "fwd_ret_5d",
    "fwd_excess_1d", "fwd_excess_3d", "fwd_excess_5d",
)

# 1トランザクションで流す更新行数の上限。Supabase の statement timeout 対策。
_UPDATE_CHUNK_SIZE = 500


def _is_finite(value) -> bool:
    """None・NaN・inf を弾く。NaN を DB へ書くと欠損が静かに汚染される。"""
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def upsert_sector_flow_features(records: list[dict]) -> int:
    """業種別フロー特徴量をUPSERTする。

    レコードは sector_flow_features.SectorFlowFeatures.to_dict() 形式。
    欠損は None のまま保存する（補間しない）。
    同一 (date, s33_code) の再投入で行数は増えない（冪等）。

    ⚠️ 将来リターン列はここでは上書きしない。特徴量の取り直しで、
       別パスが埋めた将来リターンを消してしまわないため。
       将来リターンの更新は update_sector_forward_returns() が担当する。
    """
    if not records:
        return 0

    now = datetime.utcnow()
    rows: list[dict] = []
    for r in records:
        date_value = r.get("date")
        s33_code = r.get("s33_code")
        if not date_value or not s33_code:
            logger.warning(f"業種別フロー特徴量の必須項目が欠落: {r}")
            continue

        row = {
            "date": date_value,
            "s33_code": s33_code,
            "scope": r.get("scope") or "",
            "source": r.get("source") or "JQUANTS_V2",
            "ingested_at": now,
        }
        for column in _FEATURE_COLUMNS:
            row[column] = r.get(column)

        codes = r.get("top_n_codes")
        row["top_n_codes"] = json.dumps(list(codes), ensure_ascii=False) if codes else None
        rows.append(row)

    if not rows:
        return 0

    engine = get_db_engine()
    with Session(engine) as session:
        saved = _apply_bulk_upsert(
            session, SectorFlowFeatureDaily, rows, ("date", "s33_code")
        )
        session.commit()

    logger.info(f"業種別フロー特徴量 {saved}件を保存しました")
    return saved


def update_sector_forward_returns(values_by_key: dict[str, dict]) -> int:
    """将来リターン列だけを更新する。

    Args:
        values_by_key: {"YYYY-MM-DD|S33": {"fwd_ret_1d": ..., ...}}
                       値が None のものは「まだ確定していない」として書き込まない
                       （既に入っている値を None で潰さないため）。
    """
    if not values_by_key:
        return 0

    engine = get_db_engine()
    updated = 0

    with Session(engine) as session:
        keys = [tuple(key.split("|", 1)) for key in values_by_key]
        dates = {date for date, _ in keys}
        existing = {
            (row[1], row[2]): row[0]
            for row in session.execute(
                select(
                    SectorFlowFeatureDaily.id,
                    SectorFlowFeatureDaily.date,
                    SectorFlowFeatureDaily.s33_code,
                ).where(SectorFlowFeatureDaily.date.in_(dates))
            ).all()
        }

        mappings: list[dict] = []
        for key, values in values_by_key.items():
            date_value, s33_code = key.split("|", 1)
            row_id = existing.get((date_value, s33_code))
            if row_id is None:
                continue
            payload = {
                column: values[column]
                for column in _FORWARD_COLUMNS
                if _is_finite(values.get(column))
            }
            if payload:
                mappings.append({"id": row_id, **payload})

        # bulk_update_mappings は1行1UPDATEを発行する。全期間ぶんを1トランザクション
        # で流すと Supabase の statement timeout に当たって丸ごと失敗する
        # （2026-09-01 に 8,126行で実際に発生）。分割して確定させる。
        for start in range(0, len(mappings), _UPDATE_CHUNK_SIZE):
            chunk = mappings[start:start + _UPDATE_CHUNK_SIZE]
            session.bulk_update_mappings(SectorFlowFeatureDaily, chunk)
            session.commit()
            updated += len(chunk)

    logger.info(f"将来リターンを {updated}件更新しました")
    return updated


def get_sector_flow_features_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    s33_code: Optional[str] = None,
) -> pd.DataFrame:
    """条件指定で業種別フロー特徴量を DataFrame で返す。"""
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(SectorFlowFeatureDaily).order_by(
            SectorFlowFeatureDaily.date, SectorFlowFeatureDaily.s33_code
        )
        if date:
            stmt = stmt.where(SectorFlowFeatureDaily.date == date)
        if from_date:
            stmt = stmt.where(SectorFlowFeatureDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(SectorFlowFeatureDaily.date <= to_date)
        if s33_code:
            stmt = stmt.where(SectorFlowFeatureDaily.s33_code == s33_code)

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        record = {
            "date": r.date,
            "s33_code": r.s33_code,
            "scope": r.scope,
            "top_n_codes": json.loads(r.top_n_codes) if r.top_n_codes else [],
        }
        for column in _FEATURE_COLUMNS + _FORWARD_COLUMNS:
            record[column] = getattr(r, column)
        records.append(record)

    return pd.DataFrame(records)


def get_saved_sector_feature_dates() -> list[str]:
    """保存済み業種別フロー特徴量の日付一覧を新しい順で返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(SectorFlowFeatureDaily.date)
            .distinct()
            .order_by(desc(SectorFlowFeatureDaily.date))
        ).scalars().all()
    return list(rows)


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


def save_ai_report_quality_comparison(date: str, markdown: str) -> Path:
    """AIレポート再生成の品質比較レビューをMarkdownで保存する。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"ai_report_quality_comparison_{date}.md"
    report_path.write_text(markdown, encoding="utf-8")
    logger.info(f"AIレポート品質比較レビュー保存完了: {date}")
    return report_path


def load_ai_report_quality_comparison(date: str) -> str:
    """保存済みAIレポート品質比較レビューを読み込む。"""
    report_path = REPORTS_DIR / f"ai_report_quality_comparison_{date}.md"
    if not report_path.exists():
        return ""
    return report_path.read_text(encoding="utf-8")


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


# ------------------------------------------------------------------
# 市場テーマ判定
# ------------------------------------------------------------------

def save_market_theme_snapshots(date: str, themes: list[dict]) -> int:
    """市場テーマ判定を日付単位でUPSERTする。"""
    if not themes:
        return 0

    engine = get_db_engine()
    saved = 0
    with Session(engine) as session:
        for theme in themes:
            theme_key = theme.get("key") or theme.get("theme_key") or theme.get("name")
            existing = session.execute(
                select(MarketThemeSnapshot).where(
                    MarketThemeSnapshot.date == date,
                    MarketThemeSnapshot.theme_key == theme_key,
                )
            ).scalar_one_or_none()

            values = {
                "theme_name": theme.get("name", ""),
                "score": float(theme.get("score", 0) or 0),
                "status": theme.get("status", ""),
                "confidence": theme.get("confidence", ""),
                "evidence_json": json.dumps(
                    theme.get("evidence", []), ensure_ascii=False
                ),
                "related_sectors_json": json.dumps(
                    theme.get("related_sectors", []), ensure_ascii=False
                ),
                "unverified_data_json": json.dumps(
                    theme.get("unverified_data", []), ensure_ascii=False
                ),
            }

            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                session.add(MarketThemeSnapshot(
                    date=date,
                    theme_key=theme_key,
                    **values,
                ))
            saved += 1
        session.commit()

    logger.info(f"市場テーマ判定 {saved}件を保存しました: {date}")
    return saved


def get_market_theme_snapshots(date: str) -> list[dict]:
    """指定日の市場テーマ判定を取得する。"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(MarketThemeSnapshot)
            .where(MarketThemeSnapshot.date == date)
            .order_by(desc(MarketThemeSnapshot.score))
        ).scalars().all()

    result = []
    for row in rows:
        result.append({
            "date": row.date,
            "key": row.theme_key,
            "name": row.theme_name,
            "score": row.score,
            "status": row.status,
            "confidence": row.confidence,
            "evidence": json.loads(row.evidence_json or "[]"),
            "related_sectors": json.loads(row.related_sectors_json or "[]"),
            "unverified_data": json.loads(row.unverified_data_json or "[]"),
        })
    return result


def get_market_theme_snapshot_dates(limit: int = 30) -> list[str]:
    """市場テーマ判定が保存されている日付一覧を新しい順で返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        stmt = (
            select(MarketThemeSnapshot.date)
            .distinct()
            .order_by(desc(MarketThemeSnapshot.date))
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
    return list(rows)


def save_market_news_snapshots(date: str, news_items: list[dict]) -> int:
    """ニュース検索結果を日付単位でUPSERTする。"""
    if not news_items:
        return 0

    engine = get_db_engine()
    saved = 0
    with Session(engine) as session:
        for item in news_items:
            url = item.get("url", "") or ""
            title = item.get("title", "") or ""
            existing = session.execute(
                select(MarketNewsSnapshot).where(
                    MarketNewsSnapshot.date == date,
                    MarketNewsSnapshot.url == url,
                    MarketNewsSnapshot.title == title,
                )
            ).scalar_one_or_none()

            values = {
                "query": item.get("query", ""),
                "source": item.get("source", ""),
                "published_date": item.get("published_date", ""),
                "snippet": item.get("snippet", ""),
                "score": float(item.get("score", 0) or 0),
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                session.add(MarketNewsSnapshot(
                    date=date,
                    title=title,
                    url=url,
                    **values,
                ))
            saved += 1
        session.commit()

    logger.info(f"市場ニュース {saved}件を保存しました: {date}")
    return saved


def get_market_news_snapshots(date: str) -> list[dict]:
    """指定日のニュース検索結果を取得する。"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(MarketNewsSnapshot)
            .where(MarketNewsSnapshot.date == date)
            .order_by(desc(MarketNewsSnapshot.score))
        ).scalars().all()

    return [{
        "date": row.date,
        "query": row.query,
        "title": row.title,
        "url": row.url,
        "source": row.source,
        "published_date": row.published_date,
        "snippet": row.snippet,
        "score": row.score,
    } for row in rows]


# ------------------------------------------------------------------
# 外部ナレッジ（思考データ）— 公開リポに置かず Supabase に保存
# ------------------------------------------------------------------
def upsert_knowledge_document(key: str, content: str, filename: str = "") -> None:
    """ナレッジ本文を key 単位で UPSERT する。"""
    engine = get_db_engine()
    with Session(engine) as session:
        existing = session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.key == key)
        ).scalar_one_or_none()
        if existing:
            existing.content = content
            existing.filename = filename or existing.filename
            existing.updated_at = datetime.utcnow()
        else:
            session.add(KnowledgeDocument(key=key, filename=filename, content=content))
        session.commit()


def get_knowledge_document(key: str) -> Optional[str]:
    """指定キーのナレッジ本文を返す。無ければ None。DB未接続時も例外を投げず None。"""
    try:
        engine = get_db_engine()
        with Session(engine) as session:
            row = session.execute(
                select(KnowledgeDocument.content).where(KnowledgeDocument.key == key)
            ).scalar_one_or_none()
        return row or None
    except Exception as e:  # noqa: BLE001 ナレッジ取得失敗で本処理を止めない
        logger.warning(f"ナレッジDB取得に失敗（ローカルファイルにフォールバック）: {e}")
        return None


def get_knowledge_document_keys() -> list[str]:
    """保存済みナレッジの key 一覧を返す。"""
    try:
        engine = get_db_engine()
        with Session(engine) as session:
            rows = session.execute(select(KnowledgeDocument.key)).scalars().all()
        return list(rows)
    except Exception:  # noqa: BLE001
        return []


def get_knowledge_document_meta() -> list[dict]:
    """保存済みナレッジの一覧（key・ファイル名・文字数・更新日時UTC）を返す。

    ナレッジ鮮度の表示用。DB未接続時は空リストを返し本処理を止めない。
    """
    try:
        engine = get_db_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(KnowledgeDocument).order_by(KnowledgeDocument.key)
            ).scalars().all()
            return [
                {
                    "key": row.key,
                    "filename": row.filename,
                    "chars": len(row.content or ""),
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]
    except Exception:  # noqa: BLE001
        return []


# ハウスビュー（運用者の常設の相場観）は knowledge_documents を予約キーで再利用する。
# 専用テーブルを足さないので Supabase 側のマイグレーションは不要。
HOUSE_VIEW_KEY = "__house_view__"


def save_house_view(content: str) -> None:
    """ハウスビュー本文を保存（UPSERT）する。"""
    upsert_knowledge_document(HOUSE_VIEW_KEY, content, filename="house_view")


def get_house_view() -> Optional[tuple[str, datetime]]:
    """保存済みハウスビューを (本文, 更新日時UTC) で返す。無ければ None。DB未接続も None。"""
    try:
        engine = get_db_engine()
        with Session(engine) as session:
            row = session.execute(
                select(KnowledgeDocument.content, KnowledgeDocument.updated_at)
                .where(KnowledgeDocument.key == HOUSE_VIEW_KEY)
            ).one_or_none()
        if not row or not (row[0] or "").strip():
            return None
        return (row[0], row[1])
    except Exception as e:  # noqa: BLE001 取得失敗で本処理を止めない
        logger.warning(f"ハウスビューDB取得に失敗: {e}")
        return None


# ==================================================================
# 米国ショートフロー（US-P1）
#
# 日本側の関数群とは完全に独立している。日米のデータを跨いだ計算は行わない
# （JP=業種別・売買代金JPY / US=銘柄別・株数。単位も粒度も異なる）。
# ==================================================================

def _apply_bulk_upsert(
    session,
    model,
    rows: list[dict],
    key_fields: tuple[str, ...],
    date_field: str = "date",
) -> int:
    """既存行を1回のSELECTで引き当て、まとめてINSERT/UPDATEする。

    1行ずつ SELECT+INSERT すると Supabase 相手では往復遅延が支配的になり、
    数千行のバックフィルで数十分かかる。往復回数を数回に抑えるための共通処理。
    冪等性は呼び出し側と同じ（キー重複は行を増やさず更新になる）。
    """
    if not rows:
        return 0

    # 同一バッチ内のキー重複は後勝ちで畳む（重複INSERTで制約違反にしない）
    deduped: dict[tuple, dict] = {}
    for row in rows:
        deduped[tuple(row[field] for field in key_fields)] = row
    rows = list(deduped.values())

    # 引き当て対象を日付で絞り込み、テーブル全体の走査を避ける
    dates = {row[date_field] for row in rows}
    existing_stmt = select(
        model.id, *[getattr(model, field) for field in key_fields]
    ).where(getattr(model, date_field).in_(dates))
    existing_ids = {
        tuple(record[1:]): record[0]
        for record in session.execute(existing_stmt).all()
    }

    to_insert: list[dict] = []
    to_update: list[dict] = []
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        row_id = existing_ids.get(key)
        if row_id is None:
            to_insert.append(row)
        else:
            to_update.append({**row, "id": row_id})

    if to_insert:
        session.bulk_insert_mappings(model, to_insert)
    if to_update:
        session.bulk_update_mappings(model, to_update)

    return len(rows)


def upsert_us_short_volume_records(records: list[dict]) -> int:
    """米国ショートボリュームをUPSERTする。

    レコードは finra_client.build_record() 形式の dict。
    欠落フィールドは None のまま保存する（前日値のコピーや補間は行わない）。
    同一 (date, ticker, source) の再投入で行数は増えない（冪等）。
    """
    if not records:
        return 0

    now = datetime.utcnow()
    rows: list[dict] = []
    for r in records:
        date_value = r.get("Date")
        ticker = r.get("Ticker")
        if not date_value or not ticker:
            logger.warning(f"米国ショートボリュームの必須項目が欠落: {r}")
            continue
        rows.append({
            "date": date_value,
            "ticker": ticker,
            "source": r.get("Source") or "UNKNOWN",
            "region": r.get("Region") or "US",
            "venue_scope": r.get("VenueScope") or "",
            "short_volume": r.get("ShortVolume"),
            "short_exempt_volume": r.get("ShortExemptVolume"),
            "reported_total_volume": r.get("ReportedTotalVolume"),
            "short_ratio_pct": r.get("ShortRatioPct"),
            "market_codes": r.get("MarketCodes"),
            "ingested_at": now,
        })

    engine = get_db_engine()
    with Session(engine) as session:
        saved = _apply_bulk_upsert(
            session, UsShortVolumeDaily, rows, ("date", "ticker", "source")
        )
        session.commit()

    logger.info(f"米国ショートボリューム {saved}件を保存しました")
    return saved


def get_us_short_volume_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    ticker: Optional[str] = None,
    tickers: Optional[list[str]] = None,
    source: Optional[str] = None,
) -> pd.DataFrame:
    """条件指定で米国ショートボリュームを DataFrame で返す。"""
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(UsShortVolumeDaily).order_by(
            UsShortVolumeDaily.date, UsShortVolumeDaily.ticker
        )

        if date:
            stmt = stmt.where(UsShortVolumeDaily.date == date)
        if from_date:
            stmt = stmt.where(UsShortVolumeDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(UsShortVolumeDaily.date <= to_date)
        if ticker:
            stmt = stmt.where(UsShortVolumeDaily.ticker == ticker)
        if tickers:
            stmt = stmt.where(UsShortVolumeDaily.ticker.in_(tickers))
        if source:
            stmt = stmt.where(UsShortVolumeDaily.source == source)

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "date": r.date,
        "ticker": r.ticker,
        "source": r.source,
        "venue_scope": r.venue_scope,
        "short_volume": r.short_volume,
        "short_exempt_volume": r.short_exempt_volume,
        "reported_total_volume": r.reported_total_volume,
        "short_ratio_pct": r.short_ratio_pct,
        "market_codes": r.market_codes,
    } for r in rows])


def get_us_short_volume_latest_date(source: Optional[str] = None) -> Optional[str]:
    """保存済み米国ショートボリュームの最新日付を返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        stmt = select(UsShortVolumeDaily.date)
        if source:
            stmt = stmt.where(UsShortVolumeDaily.source == source)
        stmt = stmt.order_by(desc(UsShortVolumeDaily.date)).limit(1)
        return session.execute(stmt).scalar_one_or_none()


def get_saved_us_short_volume_dates() -> list[str]:
    """保存済み米国ショートボリュームの日付一覧を新しい順で返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(UsShortVolumeDaily.date)
            .distinct()
            .order_by(desc(UsShortVolumeDaily.date))
        ).scalars().all()
    return list(rows)


def upsert_us_market_daily_records(records: list[dict]) -> int:
    """米国日足OHLCVをUPSERTする（us_price_client.build_price_record 形式）。"""
    if not records:
        return 0

    now = datetime.utcnow()
    rows: list[dict] = []
    for r in records:
        date_value = r.get("Date")
        ticker = r.get("Ticker")
        if not date_value or not ticker:
            logger.warning(f"米国日足の必須項目が欠落: {r}")
            continue
        rows.append({
            "date": date_value,
            "ticker": ticker,
            "open": r.get("Open"),
            "high": r.get("High"),
            "low": r.get("Low"),
            "close": r.get("Close"),
            "adj_close": r.get("AdjClose"),
            "market_volume": r.get("MarketVolume"),
            "ingested_at": now,
        })

    engine = get_db_engine()
    with Session(engine) as session:
        saved = _apply_bulk_upsert(session, UsMarketDaily, rows, ("date", "ticker"))
        session.commit()

    logger.info(f"米国日足 {saved}件を保存しました")
    return saved


def get_us_market_daily_df(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    ticker: Optional[str] = None,
    tickers: Optional[list[str]] = None,
) -> pd.DataFrame:
    """条件指定で米国日足OHLCVを DataFrame で返す。

    market_volume は consolidated volume。ショート比率の分母に使わないこと。
    """
    engine = get_db_engine()

    with Session(engine) as session:
        stmt = select(UsMarketDaily).order_by(
            UsMarketDaily.date, UsMarketDaily.ticker
        )

        if date:
            stmt = stmt.where(UsMarketDaily.date == date)
        if from_date:
            stmt = stmt.where(UsMarketDaily.date >= from_date)
        if to_date:
            stmt = stmt.where(UsMarketDaily.date <= to_date)
        if ticker:
            stmt = stmt.where(UsMarketDaily.ticker == ticker)
        if tickers:
            stmt = stmt.where(UsMarketDaily.ticker.in_(tickers))

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "date": r.date,
        "ticker": r.ticker,
        "open": r.open,
        "high": r.high,
        "low": r.low,
        "close": r.close,
        "adj_close": r.adj_close,
        "market_volume": r.market_volume,
    } for r in rows])


def upsert_us_short_interest_records(records: list[dict]) -> int:
    """米国の空売り残高をUPSERTする（隔週更新なので件数は少ない）。"""
    if not records:
        return 0

    now = datetime.utcnow()
    rows: list[dict] = []
    for r in records:
        settlement_date = r.get("SettlementDate")
        ticker = r.get("Ticker")
        if not settlement_date or not ticker:
            logger.warning(f"空売り残高の必須項目が欠落: {r}")
            continue
        rows.append({
            "settlement_date": settlement_date,
            "ticker": ticker,
            "issue_name": r.get("IssueName"),
            "current_short_position": r.get("CurrentShortPosition"),
            "previous_short_position": r.get("PreviousShortPosition"),
            "average_daily_volume": r.get("AverageDailyVolume"),
            "days_to_cover": r.get("DaysToCover"),
            "change_percent": r.get("ChangePercent"),
            "source": r.get("Source") or "UNKNOWN",
            "ingested_at": now,
        })

    engine = get_db_engine()
    with Session(engine) as session:
        saved = _apply_bulk_upsert(
            session, UsShortInterest, rows, ("settlement_date", "ticker", "source"),
            date_field="settlement_date",
        )
        session.commit()

    logger.info(f"米国空売り残高 {saved}件を保存しました")
    return saved


def get_us_short_interest_df(
    settlement_date: Optional[str] = None,
    tickers: Optional[list[str]] = None,
    latest_only: bool = False,
) -> pd.DataFrame:
    """米国の空売り残高をDataFrameで返す。"""
    engine = get_db_engine()

    with Session(engine) as session:
        if latest_only and not settlement_date:
            settlement_date = session.execute(
                select(UsShortInterest.settlement_date)
                .order_by(desc(UsShortInterest.settlement_date))
                .limit(1)
            ).scalar_one_or_none()
            if not settlement_date:
                return pd.DataFrame()

        stmt = select(UsShortInterest).order_by(
            UsShortInterest.settlement_date, UsShortInterest.ticker
        )
        if settlement_date:
            stmt = stmt.where(UsShortInterest.settlement_date == settlement_date)
        if tickers:
            stmt = stmt.where(UsShortInterest.ticker.in_(tickers))

        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([{
        "settlement_date": r.settlement_date,
        "ticker": r.ticker,
        "issue_name": r.issue_name,
        "current_short_position": r.current_short_position,
        "previous_short_position": r.previous_short_position,
        "average_daily_volume": r.average_daily_volume,
        "days_to_cover": r.days_to_cover,
        "change_percent": r.change_percent,
    } for r in rows])


def get_us_short_interest_latest_date() -> Optional[str]:
    """保存済み空売り残高の最新基準日を返す。"""
    engine = get_db_engine()
    with Session(engine) as session:
        return session.execute(
            select(UsShortInterest.settlement_date)
            .order_by(desc(UsShortInterest.settlement_date))
            .limit(1)
        ).scalar_one_or_none()
