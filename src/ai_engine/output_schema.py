"""
Gemini AI の出力スキーマ（Pydantic）
"""
from pydantic import BaseModel, Field
from typing import Optional


class SectorAnalysis(BaseModel):
    sector_name: str
    s33_code: str
    short_ratio_pct: float
    zone_label: str
    pro_intent: str        # 機関の真の狙い。入力データに基づく条件付き推論に限定する。
    retail_trap: str       # Retailが陥りやすい誤解
    interpretation: str    # 総合解釈。空売り比率は残高ではなく日次フローとして扱う。


class StrategicSuggestion(BaseModel):
    title: str
    target_sectors: list[str]
    strategy_type: str     # "long" | "short" | "options_call" | "options_put" | "hedge"
    rationale: str
    risk_warning: str


class DominantMarketTheme(BaseModel):
    theme_name: str
    importance: str          # "high" | "medium" | "low"
    status: str              # "主テーマ候補" | "浮上中" | "監視候補" | "要調査"
    evidence: list[str]
    impact_channels: list[str]
    related_sectors: list[str]
    short_ratio_alignment: str
    caveat: str              # 未確認データ・反証条件・過剰断定回避の注記


class ReadingReport(BaseModel):
    """AIが生成する完全解読レポートの構造"""

    # ★ Step 0必須項目（過去年パターン汚染防止）
    current_macro_context: str = Field(
        description="現在の支配的マクロ背景（イラン情勢等）を1〜3行で明記"
    )

    # 全体サマリー
    market_overall_summary: str = Field(
        description="東証全体の空売り比率の現状と意味。日次売買代金フローとして解釈し、売り残高とは表現しない"
    )

    jpx_short_selling_breakdown_analysis: str = Field(
        description="JPX公式内訳（価格規制あり/なし、規制なし構成比、実注文）の需給解釈"
    )

    price_restriction_signal: str = Field(
        description="価格規制ありを方向性売り寄り、価格規制なしをヘッジ・裁定寄りとして分解した投資シグナル。断定せず条件付きで記述"
    )

    other_category_impact: str = Field(
        description="その他（33業種外：ETF・REIT等）の市場全体への影響と無視してよいかの判断"
    )

    weekly_trend_analysis: str = Field(
        description="直近1週間のトレンド解釈"
    )

    dominant_market_themes: list[DominantMarketTheme] = Field(
        default_factory=list,
        description="市場が現在見ている主要テーマ候補。根拠、影響経路、関連業種、空売り比率との整合性を含める"
    )

    theme_shift_analysis: str = Field(
        default="市場テーマ転換の専用分析は未生成です。",
        description="前提テーマが変わりつつあるかを、根拠あり/推測/未確認を分けて条件付きで記述"
    )

    theme_sector_alignment: str = Field(
        default="市場テーマと業種別空売り比率の整合性分析は未生成です。",
        description="主要テーマと業種別空売り比率・価格規制内訳が整合するか、整合しないかを記述"
    )

    unverified_market_data: list[str] = Field(
        default_factory=list,
        description="未取得・未確認の市場データ。VIX、WTI、SOX、GEX、米金利、ドル円等を事実として断定しないために列挙"
    )

    signal_history_analysis: str = Field(
        description="機械判定シグナルの継続・新規・消滅を分析し、単日ノイズと継続フローを区別した解釈"
    )

    persistent_signal_summary: str = Field(
        description="継続シグナルの要約。何日継続しているか、需給トレンドとして重視すべき対象を明記"
    )

    new_signal_summary: str = Field(
        description="新規発生シグナルの要約。初動として監視すべき対象と反証条件を明記"
    )

    faded_signal_summary: str = Field(
        description="消滅・弱体化したシグナルの要約。売り圧力後退やノイズ化の可能性を条件付きで記述"
    )

    investment_guardrails: list[str] = Field(
        description="投資判断の安全柵。売買推奨ではないこと、空売り比率単独で判断しないこと、反証条件を確認することを3〜5項目で明記"
    )

    confirmation_conditions: list[str] = Field(
        description="翌営業日以降に確認すべき条件。シグナル継続、価格規制あり/なしの変化、市場全体との乖離などを3〜5項目で明記"
    )

    false_positive_risks: list[str] = Field(
        description="誤判定しやすいケース。ヘッジ・裁定混入、ETF/REIT等のその他影響、単日ノイズ、イベント起因の一過性フローなどを3〜5項目で明記"
    )

    additional_data_to_check: list[str] = Field(
        description="空売り比率だけでは不足するため追加確認すべきデータ。株価、出来高、先物、オプション、主体別売買、信用残などを3〜5項目で明記"
    )

    # Retail vs Pro 対比（必須）
    retail_trap: str = Field(
        description="今週の数値からRetailが陥りやすい誤解・罠"
    )
    pro_intent: str = Field(
        description="機関投資家・ヘッジファンドの真の狙いと意図"
    )

    # 業種別分析（上位5 + 下位5）
    top_sectors_analysis: list[SectorAnalysis] = Field(
        description="空売り比率が高い注目5業種の分析"
    )
    low_sectors_analysis: list[SectorAnalysis] = Field(
        description="空売り比率が低い注目5業種の分析"
    )

    # 異常値コメント
    anomaly_commentary: Optional[str] = Field(
        default=None,
        description="検知された異常値への解説（あれば）"
    )

    # 戦略的示唆
    strategic_suggestions: list[StrategicSuggestion] = Field(
        description="2〜4つの具体的な戦略示唆。入力にない価格水準や確率を作らず、反証条件を含める"
    )

    # 結論
    overall_conclusion: str = Field(
        description="参謀としての総括コメント（3〜5行）"
    )

    # メタ情報
    next_watch_points: list[str] = Field(
        description="次の監視ポイント（3〜5項目）"
    )
