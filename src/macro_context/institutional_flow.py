"""投資主体別フロー（姉妹プロジェクト jpx-analysis の週次データ）との突合。

別Supabaseプロジェクト jpx-analysis の `weekly_combined`（投資部門別の現物net・
先物net・合算、単位:億円）を読み、空売り比率レポートの Pro Intent を「実際に誰が
買い越し／売り越しているか」で裏付ける。週次データのため、対象日以前の最新週を使う。

接続は重い supabase-py を足さず、既存の requests で Supabase REST(PostgREST) を叩く。
JPX_ANALYSIS_SUPABASE_URL / _KEY 未設定、または取得失敗時は None を返し、本アプリの
動作は止めない（クロスプロジェクト依存を fail-soft にする）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests
from loguru import logger

from config.settings import (
    JPX_ANALYSIS_SUPABASE_KEY,
    JPX_ANALYSIS_SUPABASE_URL,
    MARKET_NEWS_TIMEOUT_SECONDS,
)

# jpx-analysis の investor_type → 表示ラベル（外国人→海外投資家の呼称に統一）
_INVESTOR_LABELS = {
    "foreign": "海外投資家",
    "trust_bank": "信託銀行",
    "inv_trust": "投資信託",
    "corporate": "事業法人",
    "individual": "個人",
    "dealer": "証券会社(自己)",
}
# 重要度順（機関フローの読み筋: 海外勢・信託を上に）
_ORDER = ["foreign", "trust_bank", "inv_trust", "corporate", "individual", "dealer"]


@dataclass
class InvestorFlow:
    investor_type: str
    label: str
    spot_net: float          # 現物 net（億円・+買い越し/-売り越し）
    futures_net_oku: float   # 先物 net（億円）
    combined_net: float      # 現物+先物 合算（億円）
    is_twin_engine: bool     # 現物・先物とも買い越し


@dataclass
class InvestorFlowSnapshot:
    week_date: str
    flows: list[InvestorFlow] = field(default_factory=list)


def _rest_get(path: str, params: dict) -> list[dict] | None:
    if not JPX_ANALYSIS_SUPABASE_URL or not JPX_ANALYSIS_SUPABASE_KEY:
        return None
    base = JPX_ANALYSIS_SUPABASE_URL.rstrip("/")
    headers = {
        "apikey": JPX_ANALYSIS_SUPABASE_KEY,
        "Authorization": f"Bearer {JPX_ANALYSIS_SUPABASE_KEY}",
    }
    try:
        resp = requests.get(
            f"{base}/rest/v1/{path}",
            headers=headers,
            params=params,
            timeout=MARKET_NEWS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("jpx-analysis 投資主体別フロー取得失敗: {}", exc)
        return None


def fetch_investor_flow(target_date: str) -> InvestorFlowSnapshot | None:
    """対象日以前の最新週の投資主体別フローを返す。未接続/失敗時は None。"""
    latest = _rest_get(
        "weekly_combined",
        {
            "select": "week_date",
            "week_date": f"lte.{target_date}",
            "order": "week_date.desc",
            "limit": 1,
        },
    )
    if not latest:
        return None
    week_date = latest[0].get("week_date", "")
    if not week_date:
        return None

    rows = _rest_get(
        "weekly_combined",
        {
            "select": "investor_type,spot_net,futures_net_oku,combined_net,is_twin_engine",
            "week_date": f"eq.{week_date}",
        },
    )
    if not rows:
        return None

    flows: list[InvestorFlow] = []
    for row in rows:
        it = str(row.get("investor_type", ""))
        flows.append(
            InvestorFlow(
                investor_type=it,
                label=_INVESTOR_LABELS.get(it, it),
                spot_net=float(row.get("spot_net") or 0),
                futures_net_oku=float(row.get("futures_net_oku") or 0),
                combined_net=float(row.get("combined_net") or 0),
                is_twin_engine=bool(row.get("is_twin_engine")),
            )
        )
    flows.sort(key=lambda f: _ORDER.index(f.investor_type) if f.investor_type in _ORDER else 99)
    return InvestorFlowSnapshot(week_date=week_date, flows=flows)


def diagnose_connection(target_date: str) -> dict:
    """接続診断（秘密値は返さない）。設定有無・HTTPステータス・件数・エラーを返す。"""
    info: dict = {
        "url_set": bool(JPX_ANALYSIS_SUPABASE_URL),
        "key_set": bool(JPX_ANALYSIS_SUPABASE_KEY),
        "key_prefix": (JPX_ANALYSIS_SUPABASE_KEY[:3] + "…") if JPX_ANALYSIS_SUPABASE_KEY else "",
        "url_host": "",
        "status": None,
        "rows": None,
        "error": "",
    }
    if JPX_ANALYSIS_SUPABASE_URL:
        info["url_host"] = JPX_ANALYSIS_SUPABASE_URL.rstrip("/").split("//")[-1]
    if not info["url_set"] or not info["key_set"]:
        info["error"] = "URLまたはKEYが未設定（Secretの名前・保存・Rebootを確認）"
        return info
    base = JPX_ANALYSIS_SUPABASE_URL.rstrip("/")
    headers = {
        "apikey": JPX_ANALYSIS_SUPABASE_KEY,
        "Authorization": f"Bearer {JPX_ANALYSIS_SUPABASE_KEY}",
    }
    try:
        resp = requests.get(
            f"{base}/rest/v1/weekly_combined",
            headers=headers,
            params={
                "select": "week_date",
                "week_date": f"lte.{target_date}",
                "order": "week_date.desc",
                "limit": 1,
            },
            timeout=MARKET_NEWS_TIMEOUT_SECONDS,
        )
        info["status"] = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            info["rows"] = len(data) if isinstance(data, list) else None
        else:
            info["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def build_institutional_flow_prompt_block(target_date: str) -> str:
    """プロンプトへ注入する投資主体別フローのブロックを返す。"""
    snap = fetch_investor_flow(target_date)
    if snap is None or not snap.flows:
        return (
            "【機関フロー（投資主体別・週次）】\n"
            "- データ未接続。投資主体別の裏付けは未確認として扱い、Pro Intentは断定しない。"
        )
    lines = [
        f"【機関フロー（投資主体別・週次／{snap.week_date}時点・単位:億円・jpx-analysis）】",
        "- 現物net / 先物net / 合算。プラス=買い越し、マイナス=売り越し。",
    ]
    for f in snap.flows:
        twin = "（現物・先物とも買い越し=ツインエンジン）" if f.is_twin_engine else ""
        lines.append(
            f"  {f.label}: 現物{f.spot_net:+.0f} / 先物{f.futures_net_oku:+.0f} / "
            f"合算{f.combined_net:+.0f}{twin}"
        )
    lines.append(
        "- 解釈ルール: 空売り比率の『方向性売り』を、この投資主体別フローと突合する。"
        "海外投資家が現物を買い越しているのに空売り比率が高い場合、その売りはヘッジ・裁定・"
        "個人・自己売買由来の可能性が高く、海外勢の弱気転換と断定しない。逆に海外勢が現物・"
        "先物とも売り越しなら方向性売りの裏付けが強い。週次のため当日の日次フローとは時間軸が"
        "異なる点に留意し、`institutional_flow_alignment` に整合/不整合を明記する。"
    )
    return "\n".join(lines)
