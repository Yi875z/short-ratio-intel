"""
空売り比率の集計・分析計算
"""
from typing import Optional
import pandas as pd
from loguru import logger

from config.sectors import get_zone
from src.storage.db import get_market_short_ratio_df, get_short_ratio_df


class RatioCalculator:
    """空売り比率の集計・前日比・週次サマリー計算"""

    def get_today_summary(self, target_date: str) -> dict:
        """
        指定日の全業種サマリーを返す。

        Returns:
            {
                "date": str,
                "market_ratio": float,       # 東証全体の加重平均
                "sector_data": list[dict],   # 33業種の詳細
                "top5_high": list[dict],     # 空売り比率上位5業種
                "top5_low": list[dict],      # 空売り比率下位5業種
            }
        """
        df_today = get_short_ratio_df(date=target_date)
        if df_today.empty:
            logger.warning(f"{target_date}: DBにデータなし")
            return {}

        # 前営業日データ取得（前日比計算用）
        df_prev = self._get_previous_day_df(target_date)

        sector_data = []
        for _, row in df_today.iterrows():
            prev_ratio = None
            if not df_prev.empty:
                prev_row = df_prev[df_prev["s33_code"] == row["s33_code"]]
                if not prev_row.empty:
                    prev_ratio = prev_row.iloc[0]["short_ratio_pct"]

            dod = round(row["short_ratio_pct"] - prev_ratio, 2) if prev_ratio else None
            zone = get_zone(row["short_ratio_pct"])

            sector_data.append({
                "s33_code": row["s33_code"],
                "sector_name": row["sector_name"],
                "sell_ex_short_va": row.get("sell_ex_short_va", 0),
                "shrt_with_res_va": row.get("shrt_with_res_va", 0),
                "shrt_no_res_va": row.get("shrt_no_res_va", 0),
                "total_short_va": row.get("total_short_va", 0),
                "total_volume_va": row.get("total_volume_va", 0),
                "short_ratio_pct": row["short_ratio_pct"],
                "prev_ratio_pct": prev_ratio,
                "dod_change": dod,
                "zone_key": zone["key"],
                "zone_label": zone["label"],
                "zone_color": zone["color"],
            })

        # 東証全体は公式の市場全体データを優先する。
        market_df = get_market_short_ratio_df(date=target_date)
        if not market_df.empty:
            market_row = market_df.iloc[0]
            market_ratio = market_row["short_ratio_pct"]
            market_dod = market_row["dod_change"]
            market_source = "market_total"
            market_breakdown = {
                "sell_ex_short_va": market_row.get("sell_ex_short_va", 0),
                "shrt_with_res_va": market_row.get("shrt_with_res_va", 0),
                "shrt_no_res_va": market_row.get("shrt_no_res_va", 0),
                "total_short_va": market_row.get("total_short_va", 0),
                "total_volume_va": market_row.get("total_volume_va", 0),
            }
        else:
            # 旧データ互換: 市場全体データがない場合だけ33業種平均にフォールバック。
            market_ratio = df_today["short_ratio_pct"].mean()
            market_dod = None
            market_source = "sector_average"
            market_breakdown = {}

        # ソート
        sorted_asc = sorted(sector_data, key=lambda x: x["short_ratio_pct"])
        sorted_desc = sorted(sector_data, key=lambda x: x["short_ratio_pct"], reverse=True)

        return {
            "date": target_date,
            "market_ratio": round(market_ratio, 2),
            "market_dod_change": market_dod,
            "market_source": market_source,
            "market_breakdown": market_breakdown,
            "sector_count": len(sector_data),
            "sector_data": sorted_desc,
            "top5_high": sorted_desc[:5],
            "top5_low": sorted_asc[:5],
        }

    def get_weekly_trend(self, target_date: str, days: int = 7) -> pd.DataFrame:
        """指定日から過去N日の全業種データをDataFrameで返す"""
        import pandas as pd
        from datetime import datetime, timedelta

        end = datetime.strptime(target_date, "%Y-%m-%d")
        start = end - timedelta(days=days * 2)  # 営業日換算で余裕を持つ

        df = get_short_ratio_df(
            from_date=start.strftime("%Y-%m-%d"),
            to_date=target_date,
        )
        return df

    def _get_previous_day_df(self, target_date: str) -> pd.DataFrame:
        """前営業日のデータを取得"""
        from datetime import datetime, timedelta
        dt = datetime.strptime(target_date, "%Y-%m-%d")

        for i in range(1, 5):  # 最大4日前まで探す
            prev = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
            df = get_short_ratio_df(date=prev)
            if not df.empty:
                return df
        return pd.DataFrame()
