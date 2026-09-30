"""
Gemini API へのプロンプトを動的に構築するモジュール
"""
import json
import re

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


# ── ナレッジの注入範囲（2026-09-30 に絞り込み）──────────────────────
# 以前は Vault のナレッジ7種を丸ごと（1ファイル最大1.2万字）入れており、system が約6.9万字あった。
# うち空売り比率の分析に効くのは3割程度で、残りは朝の相場レポート用の出力仕様・
# ダッシュボードのタブ構成・GEX解説（入力にGEXは無い）・ChatGPT Project の運用定型だった。
# 大きな入力は混雑時に 503 で切られやすく（3.8 は本番入力で3回連続 503、小さな入力なら即答）、
# 焦点もぼやける。そこで「見出しにこの語を含む章だけ」を入れる。
# Vault 側で見出しが変わると黙って抜けるので、見つからない語は pipeline_health が鳴らす。
KNOWLEDGE_SECTIONS: dict[str, list[str]] = {
    "project_protocol": [
        "事実・推測・シナリオの分離",
        "JPX分析の禁止・推奨表現",
        "投資主体別の時間差ルール",
        "テクニカル・クオンツ分析ルール",
        "資金フロー・四半期テーマ転換監視ルール",
    ],
    # 見出しは Vault 7/6 版と 9/13 版（NEO-OS-2.3.0）の両方に当たる語を選んでいる
    "jpx_micro": [
        "現行の市場ルール",                 # 空売り価格規制・指数リバランス・信用規制の定義
        "JPX空売り比率・価格規制内訳",
        "Market Makers / Arbitrage",        # 価格規制なし＝裁定・ヘッジの主体
        "JPX投資主体別",
    ],
    "user_rules": [
        "3.3 JPX需給",
        "3.5 空売り比率",
        "SQ・MSQ週の追加ルール",
        "AI・半導体テーマの確認ルール",
        "過去の失敗から固定する再発防止ルール",
        "データ取得不能時の回答ルール",
    ],
}
_KNOWLEDGE_SECTION_LIMIT = 6000      # 1ナレッジから入れる上限（抽出後）
_KNOWLEDGE_FALLBACK_CLIP = 3000      # 見出しが1つも当たらないときの退避（丸ごとには戻さない）


def build_system_prompt() -> str:
    """
    NEOグランドマスター人格 + ナレッジ（関係する章だけ）+ 出力スキーマを組み合わせた
    システムプロンプトを構築する。
    """
    knowledge = load_effective_knowledge()
    thresholds = SIGNAL_THRESHOLDS
    # 空白・改行なしの JSON にする（indent=2 だと約1.4倍の字数になる）
    schema_json = json.dumps(
        ReadingReport.model_json_schema(), ensure_ascii=False, separators=(",", ":")
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

## ナレッジベース（空売り比率の分析に関係する章の抜粋）

### 空売り集計（日次フロー）のプロの読み方（本アプリ専用・最優先で参照）
{_clip(knowledge.get('short_flow_pro', ''), _KNOWLEDGE_SECTION_LIMIT)}

---

### Project Operating Protocol（分析ルール抜粋）
{_extract_protocol_digest(knowledge.get('project_protocol', ''))}

---

### JPX Micro Flows（市場ルール・空売り内訳・投資主体別）
{_extract_sections(knowledge.get('jpx_micro', ''), KNOWLEDGE_SECTIONS['jpx_micro'])}

---

### User Investment Operating Rules（ユーザー固有ルール抜粋）
{_extract_sections(knowledge.get('user_rules', ''), KNOWLEDGE_SECTIONS['user_rules'])}

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
- 入力にない日経平均水準・確率・個別銘柄・前日値の断定は出力しない。入力に無い数値を「想定」「推定」で補わない
- 移動平均線・RSI などテクニカル指標は入力に無い。売買の撤退ラインや目標水準を作らない（売買推奨ではない）
- 「必ず」「完全に」「確実」「壊滅的」「歴史的」「持続不可能」「反発確率○%」などの過剰確信・誇張表現を避け、条件付きで表現する
- 機械判定の `confidence` が low の日は、結論も「〜の可能性」「〜寄り」に留め、「反転した」「決着した」と書かない
- 空売り比率の低下は「新規の空売りが減った」ことであり、それだけで「買い戻し（ショートカバー）が起きた」「ショートポジションが解消された」とは言えない。
  空売り比率から建玉・残高・ポジションの量は分からない。ショートカバーは株価上昇と組み合わせた「候補」として書く
- 各欄の字数上限（スキーマの description）を守る。同じ事実を複数の欄で繰り返さない
- `confirmation_conditions` には、翌営業日以降に確認すべき再現性・継続性の条件を具体的に書く
- `false_positive_risks` には、この解釈が外れる条件（反証条件）と、ヘッジ・裁定混入、その他（33業種外）、指数イベント、単日ノイズなどの誤判定要因を入れる
- `dominant_market_themes` には、入力された市場テーマ候補の上位1〜3件を根拠付きで入れ、`short_ratio_alignment` に関連業種の需給と整合するかを書く
- `theme_shift_analysis` には、前提テーマが変わりつつあるかを条件付きで書く
- `unverified_market_data` には、数値未取得・未確認の市場データを入れる
- `executive_summary` には、レポート全体の結論を3行以内で書く（何が起きたか・需給の主因・翌営業日の焦点）
- `regime` には「リスクオン」「リスクオフ」「レンジ・様子見」のいずれか1つだけを書く
- 投資判断ガードレール（売買推奨ではない等）は表示側で固定文を付けるので、出力しなくてよい
- `dominant_market_themes` の各テーマの `flow_classification` には資金フロー区分を1つ記す:
  Confirmed（JPX・財務省・CFTC等の公式データで確認済み）/ Price-Implied（価格・出来高・相対強度から示唆）/
  Scheduled（SQ・指数リバランス・配当等の予定された機械的フロー）/ Narrative（ニュース・期待先行）/ Unconfirmed（未確認）。
  ETF価格の上昇・相対強度だけで資金流入と断定せず、その場合は Price-Implied に留める

## 事実・解釈・推測のラベル分離（運用プロトコル準拠）

- 長文の分析フィールド（supply_demand_regime_analysis / jpx_short_selling_breakdown_analysis /
  event_calendar_context / theme_shift_analysis / institutional_flow_alignment）では、
  文の先頭に「事実:」「解釈:」「推測:」のラベルを付けて確度を分離する
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
- 業種ごとに空売り比率の平常水準は構造的に違う。「高い／低い」は固定ゾーンではなく**自己比Z・パーセンタイル**で判断し、ゾーンは目安に留める。
- 「薄商い業種」と書かれた業種（売買代金シェアが小さい）の単日の比率変化はノイズとして扱い、注目業種の主役にしない。
  注目業種は、売買代金シェアの大きい業種と、自己比Zが極端な業種・警戒ゾーンが連続している業種を優先する。
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
- 該当イベントが無い需給変化のみ、テーマ・ニュース・業種特性で説明する。イベントが効いている場合は `jpx_short_selling_breakdown_analysis` や `false_positive_risks` でその旨を明記する。

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


# 画面の _cached_sector_history(days=90) と揃える
SECTOR_HISTORY_DAYS = 90


def _sector_history_for_prompt(target_date: str, fallback_df):
    """業種の自己比（Zスコア等）用の履歴。読めなければ従来の weekly_df で続行する。"""
    try:
        from src.analyzer.ratio_calculator import RatioCalculator

        history = RatioCalculator().get_weekly_trend(target_date, days=SECTOR_HISTORY_DAYS)
        if history is not None and len(history) > 0:
            return history
    except Exception as e:  # noqa: BLE001 履歴が読めなくてもレポートは作る
        logger.warning(f"業種履歴（{SECTOR_HISTORY_DAYS}日）の取得に失敗（14日分で続行）: {e}")
    return fallback_df


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
    # Zスコア・パーセンタイル・連続日数には画面と同じ90日の履歴を使う。
    # 以前は weekly_df（14日）を渡しており、画面（90日）とAIで同じ業種のZスコアが食い違っていた。
    sector_rows = build_sector_insights(
        today_summary, _sector_history_for_prompt(target_date, weekly_df), sector_returns
    )
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
- `executive_summary` に3行以内の結論、`regime` に「リスクオン」「リスクオフ」「レンジ・様子見」のいずれか1つ。
- `supply_demand_regime_analysis` の冒頭で【需給レジーム（機械判定）】の判定名と確信度を示し、それと矛盾しないこと。
- `jpx_short_selling_breakdown_analysis` で、価格規制あり主導か・なし主導か、その他（33業種外）が市場全体を歪めているかを明記。
  当日近傍に MSCI入替・SQ・先物ロール・指数入替があれば機械的フローとして突合する。
- 機械判定シグナルとシグナル履歴は、単日ノイズと継続フローを区別するために使い、`persistent_signal_summary`・
  `new_signal_summary`・`faded_signal_summary` に各1〜2文で書く。過剰に断定せず、反証条件を `false_positive_risks` に書く。
- `institutional_flow_alignment` には、海外投資家の現物/先物のnet方向と空売りの方向性売りが一致するかを週の日付つきで書く。
  【鮮度注意】がある場合やデータ未接続時は「投資主体別の裏付けは未確認」と書き、裏付けに使わない。
- 各欄の字数上限を守り、同じ事実を複数の欄で繰り返さない。
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


_HEADING = re.compile(r"^(#{1,6})\s")


def _select_sections(text: str, keywords: list[str]) -> tuple[str, list[str]]:
    """見出しにキーワードを含む章（配下の小見出しごと）だけを抜き出す。

    Returns:
        (抜き出した本文, 1つも当たらなかったキーワード)
    章の終わりは「同じか上のレベルの次の見出し」。`#`（ファイル題）は章として選ばない。
    """
    selected: list[str] = []
    found: set[str] = set()
    keep_level: int | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            level = len(match.group(1))
            if keep_level is not None and level <= keep_level:
                keep_level = None
            if keep_level is None and level >= 2:
                hit = next((kw for kw in keywords if kw in line), None)
                if hit:
                    keep_level = level
                    found.add(hit)
        if keep_level is not None:
            selected.append(line)
    missing = [kw for kw in keywords if kw not in found]
    return "\n".join(selected).strip(), missing


def _extract_sections(
    text: str,
    keywords: list[str],
    max_chars: int = _KNOWLEDGE_SECTION_LIMIT,
) -> str:
    """ナレッジから関係する章だけを入れる。1つも当たらなければ先頭を短く切って入れる。

    丸ごと入れる退避はしない（Vault の見出し変更1つで system が元の大きさに戻るため）。
    """
    if not text:
        return "[ファイル未配置]"
    body, missing = _select_sections(text, keywords)
    if missing:
        logger.warning(f"ナレッジの見出しが見つからない（Vault側で変わった可能性）: {missing}")
    if not body:
        return _clip(text, _KNOWLEDGE_FALLBACK_CLIP)
    return _clip(body, max_chars)


def missing_knowledge_sections(knowledge: dict[str, str] | None = None) -> dict[str, list[str]]:
    """KNOWLEDGE_SECTIONS のうち、いまのナレッジに見出しが無いものを返す（点検用）。"""
    knowledge = knowledge if knowledge is not None else load_effective_knowledge()
    result: dict[str, list[str]] = {}
    for key, keywords in KNOWLEDGE_SECTIONS.items():
        text = knowledge.get(key, "")
        if not text:
            continue  # 未配置は別の問題（ローカル開発など）。ここでは鳴らさない
        _, missing = _select_sections(text, keywords)
        if missing:
            result[key] = missing
    return result


def _extract_protocol_digest(text: str, max_chars: int = _KNOWLEDGE_SECTION_LIMIT) -> str:
    """00プロトコルから分析ルールのセクションだけを抽出する（KNOWLEDGE_SECTIONS 参照）。"""
    return _extract_sections(text, KNOWLEDGE_SECTIONS["project_protocol"], max_chars)


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
