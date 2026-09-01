"""
scripts/validate_sector_features.py

Phase 1: 業種別フロー特徴量が「効いているか」を測る検証レポート。

⚠️ このスクリプトは判定ロジックを一切作らない。既に保存した特徴量と将来リターンを
   突き合わせて、統計的に差があるかどうかだけを出す。差が無ければ状態分類は作らない。

## 測るもの

1. IC（情報係数）: 各特徴量と将来超過リターンの順位相関（Spearman）
2. 五分位スプレッド: 特徴量の上位20%と下位20%で将来リターンに差が出るか
3. 条件付き: 空売り比率が異常に高かった日に絞ったときの差
4. ベースライン比較: 全日平均との差

## 統計上の注意（レポートにも明記する）

- **重複窓**: T+3 / T+5 は日付が重なるため観測は独立でない。標準誤差は過小評価になる。
- **横断相関**: 同じ日の33業種は相関する。実効サンプル数は行数よりずっと小さい。
  そのため日次集計（1日1観測）でも並行して集計する。
- **多重検定**: 特徴量 × ホライズンの数だけ検定している。単独のp値を信用しない。
- **期間の偏り**: 1年ぶんの単一レジームでしかない。別の相場つきで成り立つ保証はない。

使い方:
    python -m scripts.validate_sector_features
    python -m scripts.validate_sector_features --horizon 5
    python -m scripts.validate_sector_features --out docs/validation_phase1.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import pandas as pd
from loguru import logger

from src.storage.db import get_sector_flow_features_df, get_short_ratio_df

# 検証する特徴量（列名 → 表示名）。
# 「市場がその日の売りをどう処理したか」を表す候補だけに絞る。
FEATURES: dict[str, str] = {
    "above_vwap_pct": "VWAP超え銘柄比率",
    "high_close_pct": "高値引け比率",
    "close_above_open_pct": "終値>始値の比率",
    "close_location_median": "終値位置の中央値",
    "advancing_pct": "上昇銘柄比率",
    "top_n_turnover_share": "上位10銘柄の代金シェア",
    "short_ratio_z": "空売り比率Zスコア",
    "excess_ret_vs_topix": "当日の対TOPIX相対",
}

TARGETS = {
    1: "fwd_excess_1d",
    3: "fwd_excess_3d",
    5: "fwd_excess_5d",
}

_ZSCORE_WINDOW = 20
_MIN_SAMPLES = 16   # 窓20に対する最低サンプル（0.8）


def _load_joined() -> pd.DataFrame:
    """特徴量と業種別空売り比率を結合し、点in時点のZスコアを付ける。"""
    features = get_sector_flow_features_df()
    if features.empty:
        raise RuntimeError("業種別フロー特徴量が保存されていません。")

    short = get_short_ratio_df()
    if not short.empty:
        short = short[["date", "s33_code", "short_ratio_pct"]]
        features = features.merge(short, on=["date", "s33_code"], how="left")
    else:
        features["short_ratio_pct"] = None

    features = features.sort_values(["s33_code", "date"]).reset_index(drop=True)

    # 空売り比率のZスコアは「当日を含まない直近20営業日」で作る。
    # 当日を含めると未来を使わないまでも自分で平均を押し上げ、異常度が薄まる。
    grouped = features.groupby("s33_code")["short_ratio_pct"]
    rolling = grouped.shift(1).rolling(_ZSCORE_WINDOW, min_periods=_MIN_SAMPLES)
    mean = rolling.mean().reset_index(level=0, drop=True)
    std = rolling.std().reset_index(level=0, drop=True)
    features["short_ratio_z"] = (features["short_ratio_pct"] - mean) / std.replace(0, None)

    return features


def _spearman_ic(df: pd.DataFrame, feature: str, target: str) -> dict:
    """日ごとに順位相関を取り、その平均と標準誤差を返す（横断相関の影響を抑える）。"""
    daily = []
    for _, group in df.groupby("date"):
        pair = group[[feature, target]].dropna()
        if len(pair) < 8:      # 業種数が少なすぎる日は捨てる
            continue
        if pair[feature].nunique() < 3 or pair[target].nunique() < 3:
            continue
        # Spearman = 順位に対する Pearson。pandas の method="spearman" は scipy を
        # 要求するため、依存を増やさないよう自前で順位変換してから相関を取る。
        correlation = pair[feature].rank().corr(pair[target].rank())
        if correlation == correlation:   # NaN を除く
            daily.append(correlation)

    if len(daily) < 20:
        return {"ic": None, "days": len(daily), "t": None}

    series = pd.Series(daily)
    mean = series.mean()
    stderr = series.std(ddof=1) / (len(series) ** 0.5)
    return {
        "ic": round(mean, 4),
        "days": len(series),
        "t": round(mean / stderr, 2) if stderr else None,
    }


def _quintile_spread(df: pd.DataFrame, feature: str, target: str) -> dict:
    """特徴量の上位20%と下位20%で将来リターンを比較する。"""
    pair = df[[feature, target]].dropna()
    if len(pair) < 200:
        return {"n": len(pair)}

    try:
        pair = pair.assign(
            bucket=pd.qcut(pair[feature], 5, labels=False, duplicates="drop")
        )
    except ValueError:
        return {"n": len(pair)}

    top = pair[pair["bucket"] == pair["bucket"].max()][target]
    bottom = pair[pair["bucket"] == pair["bucket"].min()][target]
    if len(top) < 50 or len(bottom) < 50:
        return {"n": len(pair)}

    return {
        "n": len(pair),
        "top_mean": round(top.mean(), 3),
        "bottom_mean": round(bottom.mean(), 3),
        "spread": round(top.mean() - bottom.mean(), 3),
        "top_hit": round((top > 0).mean() * 100, 1),
        "bottom_hit": round((bottom > 0).mean() * 100, 1),
        "baseline_mean": round(pair[target].mean(), 3),
        "baseline_hit": round((pair[target] > 0).mean() * 100, 1),
    }


def _format_section(title: str, df: pd.DataFrame, horizon: int) -> list[str]:
    target = TARGETS[horizon]
    lines = [f"### {title}", ""]

    usable = df[df[target].notna()]
    if usable.empty:
        return lines + ["対象データなし", ""]

    lines += [
        f"対象: {len(usable):,}行 / {usable['date'].nunique()}営業日 "
        f"/ {usable['s33_code'].nunique()}業種",
        "",
        "| 特徴量 | IC(日次平均) | t値 | 上位20%平均 | 下位20%平均 | スプレッド | 上位勝率 | 下位勝率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    rows = []
    for column, label in FEATURES.items():
        if column not in usable.columns:
            continue
        ic = _spearman_ic(usable, column, target)
        spread = _quintile_spread(usable, column, target)
        rows.append((abs(spread.get("spread") or 0), column, label, ic, spread))

    for _, column, label, ic, spread in sorted(rows, key=lambda r: -r[0]):
        if "spread" not in spread:
            lines.append(f"| {label} | — | — | — | — | 判定不能(n={spread.get('n', 0)}) | — | — |")
            continue
        lines.append(
            f"| {label} | {_fmt(ic['ic'])} | {_fmt(ic['t'])} | "
            f"{spread['top_mean']:+.3f}% | {spread['bottom_mean']:+.3f}% | "
            f"**{spread['spread']:+.3f}pt** | {spread['top_hit']:.1f}% | {spread['bottom_hit']:.1f}% |"
        )

    baseline = _quintile_spread(usable, "above_vwap_pct", target)
    if "baseline_mean" in baseline:
        lines += [
            "",
            f"ベースライン（全観測）: 平均 {baseline['baseline_mean']:+.3f}% / "
            f"勝率 {baseline['baseline_hit']:.1f}%",
        ]
    lines.append("")
    return lines


def _fmt(value) -> str:
    return "—" if value is None else f"{value:+.4f}" if abs(value) < 1 else f"{value:+.2f}"


def build_report(df: pd.DataFrame, horizons: list[int]) -> str:
    lines = [
        "# Phase 1 検証レポート — 業種別フロー特徴量は効いているか",
        "",
        f"- 生成日時: {pd.Timestamp.now():%Y-%m-%d %H:%M}",
        f"- 対象期間: {df['date'].min()} 〜 {df['date'].max()}",
        f"- 観測数: {len(df):,}行（{df['date'].nunique()}営業日 × 最大{df['s33_code'].nunique()}業種）",
        "",
        "目的変数は**対TOPIX超過リターン**（業種選択の観点で意味があるのはこちら）。",
        "IC は日ごとに業種横断の順位相関を取り、その日次平均を出している"
        "（同じ日の業種同士が相関するため、行を全部プールすると実効サンプル数を過大評価する）。",
        "",
    ]

    for horizon in horizons:
        lines += [f"## T+{horizon} 営業日", ""]
        lines += _format_section("全営業日", df, horizon)

        high = df[df["short_ratio_z"] >= 1.0]
        lines += _format_section(
            "空売り比率が異常に高かった日のみ（Zスコア ≥ +1.0）", high, horizon
        )

    lines += [
        "## 読み方と注意",
        "",
        "- **重複窓**: T+3 / T+5 は観測期間が重なるため独立でない。t値は過大に出る。",
        "- **横断相関**: 同じ日の業種は同じ相場を共有する。日次ICで緩和しているが完全ではない。",
        "- **多重検定**: 特徴量×ホライズンの本数だけ検定している。単独のt値を根拠にしない。",
        "- **期間の偏り**: 1年ぶんの単一レジームにすぎない。別の相場つきでの再現は未検証。",
        "- **五分位の境界は全期間から決めている**（in-sample）。実運用では当日までの分布で"
        "決める必要があり、そのぶんここでの差は楽観側に出る。",
        "- **ICとスプレッドの符号が食い違う特徴量は、差をノイズと見なすべき**。"
        "一方は順位相関、他方は端の20%だけを見ており、両者が一致しないのは"
        "一貫した関係が無いことを示す。",
        "- **ホライズンをまたいで符号が反転する特徴量も同様にノイズ**と見なす。",
        "- スプレッドが概ね ±0.1pt 未満なら、実務上は差が無いと読むべき。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1: 業種別フロー特徴量の検証")
    parser.add_argument("--horizon", type=int, action="append",
                        help="検証するホライズン（複数可。既定: 1 3 5）")
    parser.add_argument("--out", help="Markdown の出力先")
    args = parser.parse_args()

    horizons = args.horizon or [1, 3, 5]
    df = _load_joined()
    logger.info(f"検証対象: {len(df):,}行 / {df['date'].nunique()}営業日")

    report = build_report(df, horizons)
    print(report)

    if args.out:
        path = _PROJECT_ROOT / args.out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        logger.success(f"レポートを書き出しました: {path}")


if __name__ == "__main__":
    main()
