"""
Gemini AI の出力スキーマ（Pydantic）

2026-09-30 に 31項目 → 19項目へ再編した。狙いは3つ。
- 同じことを3回書かせない（本日の結論／東証全体サマリー／総括、シグナル履歴4節など）。
- 毎日同じ文面になる定型（投資判断ガードレール）はAIに書かせず、描画側で固定表示する。
- 入力に無いデータを誘発する欄を置かない（戦略的示唆が「25日線割れで撤退」等を作っていた）。
出力量はそのまま生成時間に効くので、各欄に分量の上限を書いている。

過去に保存した report_json は旧スキーマのまま残る。読み直さないので互換は不要だが、
pydantic は未知のキーを無視するため、旧JSONを流し込んでも落ちない。
"""
from pydantic import BaseModel, Field


class SectorAnalysis(BaseModel):
    sector_name: str
    s33_code: str = ""
    short_ratio_pct: float
    zone_label: str = ""
    quadrant: str = Field(
        default="",
        description=(
            "空売り比率の前日比×株価騰落率の4象限。"
            "「売り吸収」「方向性売り」「ショートカバー候補」「買い不在」「株価未取得」のいずれか1つ"
        ),
    )
    interpretation: str = Field(
        description="1〜2文。入力の数値（比率・前日比・騰落率・規制あり/なし）を示してから解釈を条件付きで書く"
    )


class DominantMarketTheme(BaseModel):
    theme_name: str
    importance: str          # "high" | "medium" | "low"
    status: str              # "主テーマ候補" | "浮上中" | "監視候補" | "要調査"
    evidence: list[str]
    impact_channels: list[str]
    related_sectors: list[str]
    short_ratio_alignment: str = Field(
        description="このテーマと関連業種の空売り比率・4象限・価格規制内訳が整合するか。1〜2文"
    )
    caveat: str              # 未確認データ・反証条件・過剰断定回避の注記
    flow_classification: str = Field(
        default="Unconfirmed",
        description=(
            "資金フロー区分: Confirmed（公式データで確認済み）| Price-Implied（価格・出来高から示唆）| "
            "Scheduled（SQ・指数リバランス等の予定された機械的フロー）| Narrative（ニュース・期待先行）| "
            "Unconfirmed（未確認）"
        ),
    )


class ReadingReport(BaseModel):
    """AIが生成する空売り比率レポートの構造"""

    # ── 結論 ──
    executive_summary: str = Field(
        default="",
        description="3行以内。何が起きたか・需給の主因・翌営業日の焦点。機械判定レジームと矛盾させない",
    )
    regime: str = Field(
        default="",
        description='当日の市場体制。「リスクオン」「リスクオフ」「レンジ・様子見」のいずれか1つだけ',
    )
    current_macro_context: str = Field(
        default="",
        description="現在の支配的マクロ背景を1〜2文で（Step 0: 過去年パターンを投影しない）",
    )

    # ── 市場全体 ──
    supply_demand_regime_analysis: str = Field(
        default="需給レジームの専用分析は未生成です。",
        description=(
            "東証全体の需給。入力の【需給レジーム（機械判定）】の判定名を明記し、それと矛盾しないこと。"
            "比率・絶対額（空売り代金）・市場流動性（売買代金）・価格反応の4つを分けて述べ、"
            "直近の週次推移にも1文で触れる。THIN_MARKET のときは見かけの高比率として扱う。"
            "「事実:」「解釈:」「推測:」ラベルで確度を分ける。400字以内"
        ),
    )
    jpx_short_selling_breakdown_analysis: str = Field(
        default="JPX公式内訳の専用分析は未生成です。",
        description=(
            "JPX公式内訳の解釈。価格規制あり（方向性売り寄り）/なし（ヘッジ・裁定寄り）/規制なし構成比/"
            "その他（33業種外）の影響をまとめ、「方向性売り寄り」か「ヘッジ・裁定寄り」かを根拠つきで示す"
            "（集計だけで識別できない日は「識別不能」と書く）。"
            "入力に無い前日値を作らない。「事実:」「解釈:」「推測:」ラベルつき。350字以内"
        ),
    )
    event_calendar_context: str = Field(
        default="市場イベント文脈の専用分析は未生成です。",
        description=(
            "入力の市場イベント・カレンダーと当日需給の関係。MSCI入替・SQ・先物ロール・指数入替が近い場合は"
            "機械的フローとして突合する。FOMC・日銀会合が近い場合は通過前後の需給バイアスに触れる。250字以内"
        ),
    )

    # ── テーマ ──
    dominant_market_themes: list[DominantMarketTheme] = Field(
        default_factory=list,
        description="主要テーマ候補を1〜3件。根拠・影響経路・関連業種・空売り比率との整合性を含める",
    )
    theme_shift_analysis: str = Field(
        default="市場テーマ転換の専用分析は未生成です。",
        description="前提テーマ（ハウスビュー）から変わりつつあるかを、根拠あり/推測/未確認を分けて。250字以内",
    )
    institutional_flow_alignment: str = Field(
        default="投資主体別フローの突合は未生成です。",
        description=(
            "投資主体別フロー（週次）と空売りの方向性売りが整合するか。週の日付を必ず書く。"
            "入力に【鮮度注意】がある場合は裏付けに使わず「未確認」と書く。データ未接続時も未確認と明記。200字以内"
        ),
    )

    # ── 読み筋 ──
    retail_trap: str = Field(
        default="",
        description="当日の数値から個人が陥りやすい誤読を1〜2文で",
    )
    pro_intent: str = Field(
        default="",
        description="機関の狙いとして考えられるものを1〜2文で。推測であることを明示し、反証条件を添える",
    )

    # ── 業種 ──
    top_sectors_analysis: list[SectorAnalysis] = Field(
        default_factory=list,
        description="空売り比率が高い、または前日比の変化が大きい注目5業種",
    )
    low_sectors_analysis: list[SectorAnalysis] = Field(
        default_factory=list,
        description="空売り比率が低い注目3業種。top_sectors_analysis と同じ業種を重複させない",
    )

    # ── シグナル履歴 ──
    persistent_signal_summary: str = Field(
        default="継続シグナルの専用分析は未生成です。",
        description="継続シグナル（何日継続か・対象）を1〜2文で",
    )
    new_signal_summary: str = Field(
        default="新規シグナルの専用分析は未生成です。",
        description="新規シグナルを1〜2文で。翌営業日の再現性確認が必要と明記",
    )
    faded_signal_summary: str = Field(
        default="消滅・弱体化シグナルの専用分析は未生成です。",
        description="消滅・弱体化シグナルを1〜2文で。1日だけの消滅はノイズ扱い",
    )

    # ── 次に見るもの ──
    confirmation_conditions: list[str] = Field(
        default_factory=list,
        description="翌営業日以降に確認すべき条件を3〜5項目。閾値を書く場合は入力にある数値だけを使う",
    )
    false_positive_risks: list[str] = Field(
        default_factory=list,
        description="この解釈が外れる条件・誤判定しやすいケース（ヘッジ・裁定混入、指数イベント、単日ノイズ等）を2〜4項目",
    )
    unverified_market_data: list[str] = Field(
        default_factory=list,
        description="入力に無い・未確認の市場データ。事実として断定しないために列挙",
    )
