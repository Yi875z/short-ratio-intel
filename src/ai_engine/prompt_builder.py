"""
Gemini API へのプロンプトを動的に構築するモジュール
"""
import json
from loguru import logger
from src.analyzer.sector_insight import build_sector_insights, format_sector_prompt_line
from config.settings import CURRENT_MACRO_CONTEXT, MARKET_NEWS_AUTO_FETCH
from config.signal_thresholds import SIGNAL_THRESHOLDS
from src.knowledge.loader import load_effective_knowledge, load_external_knowledge
from src.macro_context.event_calendar import (
    build_event_calendar_prompt_block,
    get_events_for_date,
)
from src.macro_context.house_view import (
    build_house_view_prompt_block,
    effective_macro_context,
)
from src.macro_context.institutional_flow import build_institutional_flow_prompt_block
from src.macro_context.market_quotes import build_market_quotes_prompt_block
from src.ai_engine.output_schema import ReadingReport
from src.macro_context.context_builder import (
    build_market_context_bundle,
    build_theme_snapshot_dicts,
)
from src.macro_context.theme_history import (
    build_theme_transition_prompt_block,
    find_previous_theme_date,
)
from src.analyzer.market_breadth import DEFAULT_BREADTH_SCOPE
from src.analyzer.pressure_metrics import (
    build_pressure_metrics,
    format_pct,
    format_signed_pct,
    format_trillion_yen,
)
from src.analyzer.pressure_regime import PressureRegimeClassifier
from src.storage.db import (
    get_market_breadth_df,
    get_market_short_ratio_df,
    get_market_theme_snapshot_dates,
    get_market_theme_snapshots,
)


# 主要ナレッジ4種（global_macro / jpx_micro / options_gex / quant_psych）を
# システムプロンプトへ埋め込むときの1ファイル上限。Vault増補でプロンプトが
# 無自覚に肥大し、Gemini入力上限(250K TPM)やlost-in-the-middleを招くのを防ぐ。
_KNOWLEDGE_CLIP_CHARS = 12000


def build_system_prompt() -> str:
    """
    NEOグランドマスター人格 + ナレッジ + 出力スキーマを組み合わせた
    システムプロンプトを構築する。
    """
    knowledge = load_effective_knowledge()
    thresholds = SIGNAL_THRESHOLDS
    schema_json = json.dumps(
        ReadingReport.model_json_schema(), ensure_ascii=False, indent=2
    )

    prompt = f"""
あなたは「NEO真 金融グランドマスター 👑 The Omni-Market Sovereign」です。
日本および米国の金融市場における高度な投資分析のエキスパートとして行動してください。

## 【最重要】Step 0 プロトコル：過去年パターン汚染防止

数値を解釈する前に、必ず以下を守ること：

1. 提供される `current_macro_context` と `market_theme_context` を「今回入力された観測コンテキスト」として採用する
2. 学習データに含まれる過去の類似イベント（2025年のトランプ関税等）を
   2026年のデータに投影することは**厳禁**
3. 出力の冒頭フィールド `current_macro_context` に現在の背景を必ず明記する
4. 入力内で「未確認データ」とされた指数・金利・為替・VIX・WTI・GEX等の数値や方向性を事実として断定しない
5. 【運用者ハウスビュー】が与えられている場合、それを支配的マクロ背景の最優先アンカーとして採用する。
   当日ニュース見出しと矛盾するときはニュースを優先し、その差分を `theme_shift_analysis` に明記する。
   ハウスビューが古い/未設定の場合は、ニュース見出しと業種別データから背景を推定する。

---

## ナレッジベース

### Project Operating Protocol（最上位運用ルール・分析ルール抜粋）
{_extract_protocol_digest(knowledge.get('project_protocol', ''))}

---

### Market Preview Output Spec（市場テーマ調査・出力仕様）
{_clip(knowledge.get('market_preview_spec', ''), 12000)}

---

### Global Macro Dynamics（マクロ・為替・時間軸）
{_clip(knowledge.get('global_macro', ''), _KNOWLEDGE_CLIP_CHARS)}

---

### JPX Micro Flows（日本株・需給分析）
{_clip(knowledge.get('jpx_micro', ''), _KNOWLEDGE_CLIP_CHARS)}

---

### Options & GEX Master（オプション・ガンマ解析）
{_clip(knowledge.get('options_gex', ''), _KNOWLEDGE_CLIP_CHARS)}

---

### Quant & Psychology（クオンツ・心理学）
{_clip(knowledge.get('quant_psych', ''), _KNOWLEDGE_CLIP_CHARS)}

---

### User Investment Operating Rules（ユーザー固有・投資分析運用ルール）
{_clip(knowledge.get('user_rules', ''), 9000)}

---

## 出力フォーマット

**必ず以下のJSONスキーマに従って出力すること。他の形式は不可。**
Markdownのコードブロック（```）は使わず、純粋なJSONのみを出力すること。

{schema_json}

## 分析の鉄則

- 「Retail Trap vs Pro Intent」を必ず対比する
- 業種別解釈には機関の「テーマ売り」の文脈を明記する
- 異常値（Zスコア±2超・前日比±3pt超）には特別な注釈を付与する
- 空売り比率の現代基準は、現在の設定値では{thresholds.market_normal_lower_pct:.0f}〜{thresholds.market_warning_pct:.0f}%を通常レンジ、{thresholds.market_warning_pct:.0f}%超を警戒ラインとして判断する
- JPX空売り比率は「日次売買代金フロー」であり、「売り残高」ではない。残高と誤解される表現は禁止
- **比率と絶対額を必ず分けて述べる。**「空売り比率が上がった」と「空売り代金が増えた」は別の事実である。
  市場売買代金（分母）が縮めば、空売り代金が横ばいでも比率は上がる。
  入力の【空売り代金と市場売買代金の変化】を見ずに、比率の上昇だけを根拠に売り圧力の強化と書かない
- **入力の【需給レジーム（機械判定）】と矛盾する記述をしない。** 同じシステムが画面とレポートで
  違う結論を出すことになるため。特に判定が `THIN_MARKET` の日に「空売り比率が高く売り圧力が強い」と
  書くのは禁止。その日は商いの細りによる見かけの高比率として扱う
- 機械判定の `confidence` が low、または「未取得の入力」がある場合は、その不確かさを本文に明示する。
  未取得の入力を要する判断（例: 騰落銘柄数が未取得の日に「全面安」と断定する）は行わない
- `supply_demand_regime_analysis` には、機械判定レジームの解釈を、比率・絶対額・流動性・価格反応の
  4つに分けて書く。`regime`（リスクオン/リスクオフ/レンジ）とは別軸なので混同しない
- 入力にない日経平均水準・確率・個別銘柄の断定は出力しない
- 「必ず」「持続不可能」「反発確率○%」などの過剰確信表現を避け、条件付きで表現する
- `investment_guardrails` には、売買推奨ではないこと、空売り比率単独で判断しないこと、反証条件を確認することを必ず入れる
- `confirmation_conditions` には、翌営業日以降に確認すべき再現性・継続性の条件を具体的に書く
- `false_positive_risks` には、ヘッジ・裁定混入、その他（33業種外）、単日ノイズなどの誤判定要因を入れる
- `additional_data_to_check` には、株価・出来高・先物・オプション・主体別売買・信用残など、追加確認データを入れる
- `dominant_market_themes` には、入力された市場テーマ候補の上位1〜3件を根拠付きで入れる
- `theme_shift_analysis` には、前提テーマが変わりつつあるかを条件付きで書く
- `theme_sector_alignment` には、主要テーマと業種別空売り比率が整合するか、整合しないかを明記する
- `unverified_market_data` には、数値未取得・未確認の市場データを入れる
- `executive_summary` には、レポート全体の結論を3行以内で書く（何が起きたか・需給の主因・翌営業日の焦点）
- `regime` には「リスクオン」「リスクオフ」「レンジ・様子見」のいずれか1つだけを書く
- `dominant_market_themes` の各テーマの `flow_classification` には資金フロー区分を1つ記す:
  Confirmed（JPX・財務省・CFTC等の公式データで確認済み）/ Price-Implied（価格・出来高・相対強度から示唆）/
  Scheduled（SQ・指数リバランス・配当等の予定された機械的フロー）/ Narrative（ニュース・期待先行）/ Unconfirmed（未確認）。
  ETF価格の上昇・相対強度だけで資金流入と断定せず、その場合は Price-Implied に留める

## 事実・解釈・推測のラベル分離（運用プロトコル準拠）

- 長文の分析フィールド（market_overall_summary / jpx_short_selling_breakdown_analysis / price_restriction_signal /
  other_category_impact / event_calendar_context / weekly_trend_analysis / theme_shift_analysis /
  institutional_flow_alignment / pro_intent）では、文の先頭に「事実:」「解釈:」「推測:」のラベルを付けて確度を分離する
- 「事実:」は入力データ・報道ベースで確認できる内容のみ。「解釈:」はデータから合理的に読める意味。
  「推測:」は可能性の指摘であり、反証条件をセットで書く

## JPX公式内訳の解釈ルール

- 総空売り比率 = (空売り・価格規制あり + 空売り・価格規制なし) / 合計売買代金
- 価格規制ありは「方向性売り・通常の空売り圧力」に近いシグナルとして扱う
- 価格規制なしは「裁定・ヘッジ・流動性供給」を含みやすく、単独で弱気売りと断定しない
- 規制なし構成比が高い場合は、ベア圧力よりもヘッジ/裁定フローの混入を疑う
- 「その他（33業種外）」はETF・REIT等を含むため、指数ヘッジやパッシブ/裁定フローの影響として必ず別枠で評価する
- レポートでは「方向性売り主導」か「ヘッジ・裁定主導」かを明確に分類する
- 価格規制ありが高くても「機関の確信的売り」と断定しない。マクロ、前日比、週次推移、業種特性を合わせて「方向性売り寄り」と表現する

## 業種別の空売り比率×株価の4象限ルール（最重要）

- 業種別データには、空売り比率の前日比（pt）と、同じ業種の株価指数の前日騰落率（%）が併記される。
  **比率の水準だけで弱気と判断してはならない。必ず株価の反応と組み合わせて読む。**
- 4象限の読み分け（いずれも可能性であり断定しない）:
  - 比率上昇 × 株価上昇 = 売りが吸収されている。踏み上げ・押し目買い優勢の可能性。
    ここを「高い空売り比率＝弱気」と読むのは誤り。売り方が劣勢な場面である可能性を先に検討する。
  - 比率上昇 × 株価下落 = 方向性売り優勢の可能性。ただし規制なし構成比が高ければヘッジ・裁定の混入を疑う。
  - 比率低下 × 株価上昇 = ショートカバー主導の可能性。新規の買いではなく買い戻しで上げている場合、
    カバーが一巡すると上昇の勢いが続かない可能性を併記する。
  - 比率低下 × 株価下落 = 売り圧力は後退しているが買いが不在の可能性。売り方の撤退を強気材料と即断しない。
- 株価が「N/A」の業種は騰落率を取得できていない。その業種では象限を断定せず、比率のみの解釈に留める。
- 主要テーマに該当する業種（半導体なら電気機器・精密機器など）は、必ずこの4象限の言葉で説明する。

## シグナル履歴の解釈ルール

- 継続シグナルは単日ノイズより重視する。現在の設定値では{thresholds.persistent_signal_days}営業日以上継続したものは需給トレンドとして扱う
- 新規シグナルは初動候補であり、翌営業日の再現性確認を必ず条件に入れる
- 消滅シグナルは売り圧力後退の可能性。ただし1日だけの消滅はノイズ扱いにする
- 戦略示唆では、継続シグナルは「順張り・警戒継続」、新規シグナルは「監視・小さく試す」、消滅シグナルは「反転確認待ち」と分ける
- シグナル履歴を使う場合も、空売り比率は残高ではなく日次フローである点を維持する

## 市場イベント・カレンダーの解釈ルール
- 入力の【市場イベント・カレンダー】を解釈の前提に使う。MSCI入替・SQ・先物ロールが当日〜数日内にある場合、その他（33業種外）の急騰や価格規制なし比率の上昇は、まずインデックス連動の機械的フロー（パッシブ・裁定）で説明できないかを最優先で検討し、方向性売り（弱気）と断定しない。
- FOMC・日銀会合の直前は、リスク回避のヘッジ・ショート積み増しが起きやすく、通過後は巻き戻し（ショートカバー）が起きやすい。イベント前の空売り比率上昇を「確信的な弱気」と断定しない。
- 該当イベントが無い需給変化のみ、テーマ・ニュース・業種特性で説明する。イベントが効いている場合は `other_category_impact` や `false_positive_risks` でその旨を明記する。

## 支配的マクロ背景のレジーム裁定（重要）
- 冒頭の `current_macro_context` では、当日を「リスクオン／リスクオフ／レンジ・様子見」のいずれの体制かを1つ明示する（両論併記で終わらせない）。
- 米金利低下期待（Fed緩和）と、原油・地政学によるインフレ再燃（金利上昇）のような競合ナラティブが併存する場合は、当日のニュース見出し・業種別需給・イベント予定からどちらが優勢かを裁定し、劣勢側は「リスク要因」として位置づける。
- ハウスビューの体制観と当日データが食い違う場合は、`theme_shift_analysis` で「ハウスビューはX体制だが当日はY寄り」と差分を明示する。

## テーマ判定の追加ルール（重要）
- 主要テーマの根拠に「業種別空売り比率の高さ」を使うときは、継続シグナル（長期間継続して高い業種）を根拠から除外する。長期継続の高空売りは当日テーマではなく構造的な需給であり、特定ニュースの裏付けに流用しない。テーマ整合は「前日比の急変」「新規発生シグナル」で評価する。
- 運用者ハウスビューや主役テーマが半導体・AI・グロースを挙げている場合は、電気機器・精密機器・情報・通信業の需給を必ず個別に解説する。低空売りでも「売り手が攻めあぐねている／押し目買い意欲が強い」等の含意を述べ、主役テーマを放置しない。

## ニュース見出しの数値の扱い
- ニュース見出しに具体的数値（為替水準・金利/利回り・指数値）が含まれる場合は「報道ベース」と明示して引用してよく、`unverified_market_data` には入れない。
- 見出しに数値が無い指標のみ `unverified_market_data` に列挙する。報道で水準が判明しているものを未確認扱いしない。
"""
    logger.info(f"プロンプト規模: system={len(prompt):,}字")
    return prompt


def build_pressure_regime_prompt_block(target_date: str) -> str:
    """需給モニターの機械判定を、AIレポートへ渡すブロックとして組み立てる。

    画面（需給モニタータブ）とレポート本文が食い違わないようにするのが目的。
    機械判定が THIN_MARKET（商いが細って比率だけ高い）と言っている日に、
    レポートが「空売り比率が高く売り圧力が強い」と書くと、同じシステムが
    2つの結論を出すことになる。

    絶対額の変化（前日比・5日平均比・Zスコア）と市場売買代金の推移も渡す。
    従来のプロンプトは比率の水準しか渡しておらず、「分母が縮んだだけ」を
    AIが判定する材料が無かった。

    ⚠️ fail-soft。データが無い・計算できない場合も空文字を返さず、
    「未接続」と明示したブロックを返す（AIが黙って推測で埋めないため）。
    ⚠️ 業種別フロー特徴量（Phase 0）はここに含めない。まだ検証前であり、
    未検証の指標をAIに解釈させると根拠のない断定を生む。
    """
    try:
        market_df = get_market_short_ratio_df(to_date=target_date)
        if market_df is None or market_df.empty:
            return "【需給レジーム（機械判定）】:\n  データなし（空売り集計が未取得）"

        breadth_row = None
        breadth_df = get_market_breadth_df(
            date=target_date, market_scope=DEFAULT_BREADTH_SCOPE
        )
        if breadth_df is not None and not breadth_df.empty:
            breadth_row = breadth_df.iloc[0].to_dict()

        metrics = build_pressure_metrics(target_date, market_df, breadth_row)
        result = PressureRegimeClassifier().classify(metrics)

        lines = [
            "【需給レジーム（機械判定・この判定と矛盾する記述をしないこと）】:",
            f"  判定: {result.primary}（{result.primary_label}） / 確信度: {result.confidence}",
            f"  定義: {result.description}",
        ]
        for reason in result.reasons:
            lines.append(f"  根拠: {reason}")
        for caveat in result.caveats:
            lines.append(f"  注意: {caveat}")
        if result.also_matched:
            lines.append(f"  同時成立: {', '.join(result.also_matched)}")
        if result.missing_inputs:
            lines.append(
                f"  未取得の入力: {' / '.join(result.missing_inputs)}"
                "（これを必要とするレジームは判定していない）"
            )

        values = metrics.values
        short_change = metrics.short_value_change
        volume_change = metrics.market_volume_change
        lines += [
            "",
            "【空売り代金と市場売買代金の変化（比率とは別の情報）】:",
            f"  総空売り代金: {format_trillion_yen(values.total_short_va)} / "
            f"前日比 {_dod_text(short_change)} / "
            f"5日平均比 {format_signed_pct(short_change.vs_avg_pct, 1)} / "
            f"Zスコア {_z_text(short_change)}",
            f"  市場売買代金: {format_trillion_yen(values.market_volume_va)} / "
            f"前日比 {_dod_text(volume_change)} / "
            f"5日平均比 {format_signed_pct(volume_change.vs_avg_pct, 1)} / "
            f"Zスコア {_z_text(volume_change)}",
            f"  空売り比率のZスコア: {_z_text(metrics.total_ratio_change)} / "
            f"価格規制あり比率のZスコア: {_z_text(metrics.with_ratio_change)}",
        ]

        price = metrics.price
        breadth = metrics.breadth
        lines += [
            "",
            "【価格反応と市場の広がり】:",
            f"  TOPIX当日騰落率: {format_signed_pct(price.topix_change_pct, 2)}"
            if price.available else "  TOPIX当日騰落率: 未取得",
        ]
        if breadth.available:
            lines.append(
                f"  {breadth.scope_label}: 値上がり{breadth.advancing}銘柄 / "
                f"値下がり{breadth.declining}銘柄 / "
                f"ネットブレッドス {breadth.net_breadth:+.3f}"
                "（空売り集計とは対象市場が異なるため、割り算せず並べて読むこと）"
            )
        else:
            lines.append("  騰落銘柄数: 未取得")

        return "\n".join(lines)

    except Exception as exc:  # noqa: BLE001 レポート生成を止めない
        logger.warning(f"需給レジームブロックの構築に失敗（レポートは継続）: {exc}")
        return "【需給レジーム（機械判定）】:\n  取得失敗（判定なしとして扱うこと）"


def _dod_text(change) -> str:
    """前日比。算出できない日は「—」で済ませず理由まで書く。

    AIは「—」を0や横ばいと読み替えることがある。前営業日が欠測しているのか、
    値が動かなかったのかは別の事実なので、区別して伝える。
    """
    if change.dod_pct is None:
        return "—（前営業日が未取得で算出不能。横ばいという意味ではない）"
    return format_signed_pct(change.dod_pct, 1)


def _z_text(change) -> str:
    if change is None or change.zscore is None:
        return "N/A（サンプル不足）"
    return f"{change.zscore:+.2f}"


def _safe_sector_returns(target_date: str) -> dict:
    """業種別騰落率を取得する。失敗しても空辞書を返し、従来の組み立てを続ける。"""
    try:
        from src.macro_context.sector_price import returns_by_sector_code

        return returns_by_sector_code(target_date)
    except Exception as e:  # noqa: BLE001 株価が取れなくてもレポートは作る
        logger.warning(f"業種別騰落率の取得に失敗（比率のみで続行）: {e}")
        return {}


def build_user_prompt(
    target_date: str,
    today_summary: dict,
    weekly_df,
    anomalies: list,
    extra_news: str = "",
    auto_fetch_news: bool | None = None,
    quality_feedback: str = "",
) -> str:
    """
    当日データ・週次推移・異常値を組み合わせたユーザープロンプト。
    """
    # 業種別株価指数の前日騰落率（取得できなければ従来どおり比率のみで組み立てる）
    sector_returns = _safe_sector_returns(target_date)

    # セクターデータを整形。計算は sector_insight に集約してあり、
    # Streamlit の業種タブが表示するのと同じ数字をここでも使う（AIと画面の食い違い防止）。
    sector_rows = build_sector_insights(today_summary, weekly_df, sector_returns)
    sector_table = "\n".join(format_sector_prompt_line(row) for row in sector_rows)

    # 週次推移（JPX公式の市場全体データを優先）
    weekly_summary = ""
    market_trend_df = get_market_short_ratio_df(to_date=target_date)
    if not market_trend_df.empty:
        market_trend_df = market_trend_df.sort_values("date").tail(10)
        for _, row in market_trend_df.iterrows():
            dod = row.get("dod_change")
            dod_str = f"{dod:+.1f}pt" if dod is not None else "N/A"
            weekly_summary += (
                f"  {row['date']}: {row['short_ratio_pct']:.1f}% "
                f"(前日比 {dod_str})\n"
            )
    elif not weekly_df.empty:
        for dt, group in weekly_df.groupby("date"):
            avg = group["short_ratio_pct"].mean()
            weekly_summary += f"  {dt}: {avg:.1f}%（33業種平均・参考）\n"

    # 異常値リスト
    anomaly_text = ""
    if anomalies:
        for a in anomalies:
            anomaly_text += f"  ⚠️ [{a.severity.upper()}] {a.sector_name}: {a.description}\n"
    else:
        anomaly_text = "  検知なし"

    signal_text = ""
    flow_signals = today_summary.get("flow_signals", [])
    if flow_signals:
        for sig in flow_signals[:10]:
            details = " / ".join(str(item) for item in sig.get("details", []))
            invalidation = sig.get("invalidation_condition", "")
            signal_text += (
                f"  [{sig.get('severity', 'medium').upper()}] "
                f"{sig.get('category', '')}/{sig.get('target', '')}: "
                f"{sig.get('signal', '')} - {sig.get('rationale', '')} "
                f"判定根拠: {details if details else 'N/A'} "
                f"確認点: {sig.get('watch_point', '')} "
                f"反証条件: {invalidation if invalidation else 'N/A'}\n"
            )
    else:
        signal_text = "  検知なし"

    history_text = ""
    flow_signal_history = today_summary.get("flow_signal_history", [])
    if flow_signal_history:
        for item in flow_signal_history[:12]:
            streak = item.get("streak_days", 0)
            history_text += (
                f"  [{item.get('state', '')}] {item.get('category', '')}/"
                f"{item.get('target', '')}: {item.get('signal', '')} "
                f"発生日数{item.get('active_days', 0)}日"
            )
            if item.get("state") in ["継続", "新規"]:
                history_text += f" / 継続{streak}日"
            history_text += f" / 最終確認{item.get('last_seen', '')}\n"
    else:
        history_text = "  データなし"

    market_breakdown = today_summary.get("market_breakdown", {})
    breakdown_text = "  データなし"
    total_volume = market_breakdown.get("total_volume_va", 0)
    short_with = market_breakdown.get("shrt_with_res_va", 0) or 0
    short_without = market_breakdown.get("shrt_no_res_va", 0) or 0
    total_short = market_breakdown.get("total_short_va", short_with + short_without) or 0
    actual = market_breakdown.get("sell_ex_short_va", 0) or 0

    # ⚠️ 合計売買代金はスクレイパーからも取れる。それだけを条件にすると、
    # 内訳が無い日に「空売り0百万円 (0.0%)」という事実に反する行をAIへ渡してしまう。
    # 同じプロンプト内の需給レジームは「未取得」と書くため、主張が矛盾する。
    if total_volume and not (short_with or short_without or total_short):
        breakdown_text = (
            "  JPX内訳: 未取得（この日は空売り比率と売買代金のみ取得済み）\n"
            f"  売買代金合計: {total_volume:,.0f}百万円"
        )
    elif total_volume:
        with_ratio = short_with / total_volume * 100
        without_ratio = short_without / total_volume * 100
        without_share = short_without / total_short * 100 if total_short else 0
        actual_ratio = actual / total_volume * 100 if total_volume else 0
        breakdown_text = (
            f"  実注文売買代金: {actual:,.0f}百万円 ({actual_ratio:.1f}%)\n"
            f"  空売り（価格規制あり）: {short_with:,.0f}百万円 "
            f"({with_ratio:.1f}%)\n"
            f"  空売り（価格規制なし）: {short_without:,.0f}百万円 "
            f"({without_ratio:.1f}%)\n"
            f"  規制なし構成比: {without_share:.1f}%\n"
            f"  売買代金合計: {total_volume:,.0f}百万円"
        )

    other = next(
        (s for s in today_summary.get("sector_data", []) if s.get("s33_code") == "9999"),
        None,
    )
    other_text = "  データなし"
    if other:
        other_volume = other.get("total_volume_va", 0) or 0
        other_with = other.get("shrt_with_res_va", 0) or 0
        other_without = other.get("shrt_no_res_va", 0) or 0
        other_short = other.get("total_short_va", other_with + other_without) or 0
        market_volume = market_breakdown.get("total_volume_va", 0) or 0
        other_text = (
            f"  その他（33業種外）: 総空売り{other['short_ratio_pct']:.1f}% / "
            f"規制あり{(other_with / other_volume * 100) if other_volume else 0:.1f}% / "
            f"規制なし{(other_without / other_volume * 100) if other_volume else 0:.1f}% / "
            f"規制なし構成比{(other_without / other_short * 100) if other_short else 0:.1f}% / "
            f"市場売買代金シェア{(other_volume / market_volume * 100) if market_volume else 0:.1f}%"
        )

    # 支配的マクロ背景の起点は「運用者ハウスビュー」を最優先。無ければ固定ベースライン。
    effective_baseline, baseline_source = effective_macro_context()
    house_view_block = build_house_view_prompt_block()
    event_calendar_block = build_event_calendar_prompt_block(target_date)
    sq_week_case_block = _build_sq_week_case_block(target_date)
    institutional_flow_block = build_institutional_flow_prompt_block(target_date)
    live_market_block = build_market_quotes_prompt_block()
    pressure_regime_block = build_pressure_regime_prompt_block(target_date)

    market_context = build_market_context_bundle(
        target_date=target_date,
        today_summary=today_summary,
        manual_news=extra_news,
        baseline_context=effective_baseline,
        auto_fetch_news=(
            auto_fetch_news if auto_fetch_news is not None else MARKET_NEWS_AUTO_FETCH
        ),
    )
    theme_transition_context = build_theme_transition_context_for_prompt(
        target_date=target_date,
        today_summary=today_summary,
        current_news_text=market_context.combined_news_text,
        baseline_context=effective_baseline,
    )
    quality_feedback_block = ""
    if quality_feedback:
        quality_feedback_block = (
            "【前回品質チェックからの改善指示】:\n"
            f"{quality_feedback}"
        )

    prompt = f"""
【分析対象日】: {target_date}

{house_view_block}

{event_calendar_block}

{sq_week_case_block}

{institutional_flow_block}

{live_market_block}

【現在の支配的マクロ背景・市場テーマ判定】:
{market_context.to_prompt_block()}

【市場テーマ履歴・転換メモ】:
{theme_transition_context}

{quality_feedback_block}

{f'【本日の追加ニュース】:{extra_news}' if extra_news else ''}

【東証全体の空売り比率】: {today_summary.get('market_ratio', 'N/A')}%

{pressure_regime_block}

【JPX空売り内訳】:
{breakdown_text}

【その他（33業種外）の影響】:
{other_text}

【週次推移（直近）】:
{weekly_summary if weekly_summary else '  データなし'}

【業種別データ（高い順、JPX内訳＋株価騰落率＋4象限）】:
{sector_table}

【検知された異常値】:
{anomaly_text}

【機械判定シグナル】:
{signal_text}

【シグナル履歴】:
{history_text}

【表現ルール】:
  - 空売り比率は日次フロー。売り残高・残高・建玉と表現しない。
  - 入力にない価格水準や発生確率を作らない。
  - 強い示唆は「条件」「必要な確認材料」「反証条件」とセットで書く。
  - レポートは売買推奨ではなく、需給分析の補助材料として書く。
  - 新規シグナルは「翌営業日の再現性確認が必要」と明記する。
  - ヘッジ・裁定・ETF/REIT由来のフローを、方向性売りと混同しない。
  - 市場テーマは、根拠あり・推測・未確認を分けて扱う。
  - 上部の「ライブ市場気配（実測）」に載っている値（日経/TOPIX/ナスダック先物・ドル円・米10年/30年金利・VIX・WTI・SOX等）は実測として扱ってよい。そこに無い指標やGEXは実測済みデータとして断定しない。

上記データを NEO真金融グランドマスター として分析し、
「空売り比率 完全解読レポート」を指定のJSONフォーマットで出力してください。
出力では `executive_summary` に3行以内の結論、`regime` に「リスクオン」「リスクオフ」「レンジ・様子見」のいずれか1つを必ず記述してください。
特に、価格規制あり主導なのか、価格規制なし主導なのか、その他（33業種外）が市場全体を歪めているかを必ず明記してください。
機械判定シグナルは結論の補助材料として使い、過剰に断定せず、反証条件も含めてください。
シグナル履歴は、単日ノイズと継続フローを区別するために使ってください。
出力では `signal_history_analysis`、`persistent_signal_summary`、`new_signal_summary`、`faded_signal_summary` に必ず履歴分析を記述してください。
出力では `investment_guardrails`、`confirmation_conditions`、`false_positive_risks`、`additional_data_to_check` に必ず投資判断ガードレールを記述してください。
出力では `dominant_market_themes`、`theme_shift_analysis`、`theme_sector_alignment`、`unverified_market_data` に必ず市場テーマ判定を記述してください。
出力では `event_calendar_context` に市場イベント・カレンダーと当日需給の関係を必ず記述してください。特に「その他（33業種外）」の急騰や価格規制なし比率の上昇は、当日近傍のMSCI入替・SQ・先物ロールがあれば機械的フローとして突合し、`other_category_impact` にもその旨を明記してください。
出力では `institutional_flow_alignment` に、Pro Intent（機関の狙い）が【機関フロー（投資主体別・週次）】と整合するかを必ず記述してください。海外投資家の現物/先物のnet方向と、空売り比率の方向性売りが一致するか・しないかを明示し、一致しない場合は売りの主体（ヘッジ/裁定/個人/自己売買）を推定してください。データ未接続時は「投資主体別の裏付けは未確認」と明記してください。
"""
    logger.info(f"プロンプト規模: user={len(prompt):,}字")
    return prompt


def build_theme_transition_context_for_prompt(
    target_date: str,
    today_summary: dict,
    current_news_text: str = "",
    baseline_context: str = CURRENT_MACRO_CONTEXT,
) -> str:
    """
    保存済み市場テーマ履歴をAIプロンプト用の転換メモへ変換する。

    対象日の保存済みテーマがない場合は、今回の入力文脈から一時的に
    テーマ判定を作り、前回保存テーマと比較する。DBへは保存しない。
    """
    theme_dates = sorted(get_market_theme_snapshot_dates(limit=30))
    previous_date = find_previous_theme_date(theme_dates, target_date)
    previous_themes = get_market_theme_snapshots(previous_date) if previous_date else []

    current_themes = get_market_theme_snapshots(target_date)
    current_source = "saved_snapshot"
    if not current_themes:
        current_themes = build_theme_snapshot_dicts(
            target_date,
            today_summary,
            manual_news=current_news_text,
            baseline_context=baseline_context,
        )
        current_source = "generated_for_prompt_only"

    return build_theme_transition_prompt_block(
        target_date=target_date,
        current_themes=current_themes,
        previous_themes=previous_themes,
        previous_date=previous_date,
        current_source=current_source,
    )


def _clip(text: str, max_chars: int) -> str:
    """巨大ナレッジをプロンプトへ入れるときの上限をかける。"""
    if not text:
        return "[ファイル未配置]"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...以下、長文のため省略...]"


# 00プロトコルのうち、本アプリのレポート生成に効く分析ルールの見出しキーワード。
# モード定義・ChatGPT Project運用（前半）はアプリでは不要なため抽出しない。
_PROTOCOL_DIGEST_KEYWORDS = [
    "事実・推測・シナリオの分離",
    "JPX分析の禁止・推奨表現",
    "投資主体別の時間差ルール",
    "J-NET判定ルール",
    "GEX・オプション分析ルール",
    "Global Macro分析ルール",
    "テクニカル・クオンツ分析ルール",
    "資金フロー・四半期テーマ転換監視ルール",
    "心理・資金管理ルール",
]


def _extract_protocol_digest(text: str, max_chars: int = 12000) -> str:
    """00プロトコルから分析ルールのセクションだけを抽出する。

    旧実装は先頭からの単純クリップで、19,000字超の新版00では後半の
    分析ルール（事実/推測分離・資金フロー区分等）が切り捨てられていた。
    見出し構成が変わって1つも抽出できない場合は従来のクリップに戻す。
    """
    if not text:
        return "[ファイル未配置]"
    sections: list[str] = []
    keep = False
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if keep and buf:
                sections.append("\n".join(buf).strip())
            buf = [line]
            keep = any(kw in line for kw in _PROTOCOL_DIGEST_KEYWORDS)
        elif keep:
            buf.append(line)
    if keep and buf:
        sections.append("\n".join(buf).strip())
    if not sections:
        return _clip(text, max_chars)
    return _clip("\n\n".join(sections), max_chars)


def _build_sq_week_case_block(target_date: str) -> str:
    """SQ・MSQ週に限り、過去事例・再発防止ルール（Vault 07）を注入する。

    通常日はプロンプト肥大とGeminiクォータ消費を避けるため注入しない。
    """
    events = get_events_for_date(target_date, before_days=2, after_days=5)
    if not any(e.category in ("sq", "rollover") for e in events):
        return ""
    past_cases = load_external_knowledge("past_cases")
    if not past_cases:
        return ""
    return (
        "【SQ・MSQ週の過去事例・再発防止ルール】:\n"
        "対象日はSQ・MSQ週の近傍です。以下の過去事例の教訓を解釈に反映してください。\n"
        + _clip(past_cases, 9000)
    )
