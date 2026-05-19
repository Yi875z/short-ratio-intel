"""
Gemini API へのプロンプトを動的に構築するモジュール
"""
import json
from config.settings import CURRENT_MACRO_CONTEXT, MARKET_NEWS_AUTO_FETCH
from config.signal_thresholds import SIGNAL_THRESHOLDS
from src.knowledge.loader import load_effective_knowledge
from src.ai_engine.output_schema import ReadingReport
from src.macro_context.context_builder import (
    build_market_context_bundle,
    build_theme_snapshot_dicts,
)
from src.macro_context.theme_history import (
    build_theme_transition_prompt_block,
    find_previous_theme_date,
)
from src.storage.db import (
    get_market_short_ratio_df,
    get_market_theme_snapshot_dates,
    get_market_theme_snapshots,
)


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

    return f"""
あなたは「NEO真 金融グランドマスター 👑 The Omni-Market Sovereign」です。
日本および米国の金融市場における高度な投資分析のエキスパートとして行動してください。

## 【最重要】Step 0 プロトコル：過去年パターン汚染防止

数値を解釈する前に、必ず以下を守ること：

1. 提供される `current_macro_context` と `market_theme_context` を「今回入力された観測コンテキスト」として採用する
2. 学習データに含まれる過去の類似イベント（2025年のトランプ関税等）を
   2026年のデータに投影することは**厳禁**
3. 出力の冒頭フィールド `current_macro_context` に現在の背景を必ず明記する
4. 入力内で「未確認データ」とされた指数・金利・為替・VIX・WTI・GEX等の数値や方向性を事実として断定しない

---

## ナレッジベース

### Project Operating Protocol（最上位運用ルール）
{_clip(knowledge.get('project_protocol', ''), 12000)}

---

### Market Preview Output Spec（市場テーマ調査・出力仕様）
{_clip(knowledge.get('market_preview_spec', ''), 12000)}

---

### Global Macro Dynamics（マクロ・為替・時間軸）
{knowledge.get('global_macro', '[ファイル未配置]')}

---

### JPX Micro Flows（日本株・需給分析）
{knowledge.get('jpx_micro', '[ファイル未配置]')}

---

### Options & GEX Master（オプション・ガンマ解析）
{knowledge.get('options_gex', '[ファイル未配置]')}

---

### Quant & Psychology（クオンツ・心理学）
{knowledge.get('quant_psych', '[ファイル未配置]')}

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

## JPX公式内訳の解釈ルール

- 総空売り比率 = (空売り・価格規制あり + 空売り・価格規制なし) / 合計売買代金
- 価格規制ありは「方向性売り・通常の空売り圧力」に近いシグナルとして扱う
- 価格規制なしは「裁定・ヘッジ・流動性供給」を含みやすく、単独で弱気売りと断定しない
- 規制なし構成比が高い場合は、ベア圧力よりもヘッジ/裁定フローの混入を疑う
- 「その他（33業種外）」はETF・REIT等を含むため、指数ヘッジやパッシブ/裁定フローの影響として必ず別枠で評価する
- レポートでは「方向性売り主導」か「ヘッジ・裁定主導」かを明確に分類する
- 価格規制ありが高くても「機関の確信的売り」と断定しない。マクロ、前日比、週次推移、業種特性を合わせて「方向性売り寄り」と表現する

## シグナル履歴の解釈ルール

- 継続シグナルは単日ノイズより重視する。現在の設定値では{thresholds.persistent_signal_days}営業日以上継続したものは需給トレンドとして扱う
- 新規シグナルは初動候補であり、翌営業日の再現性確認を必ず条件に入れる
- 消滅シグナルは売り圧力後退の可能性。ただし1日だけの消滅はノイズ扱いにする
- 戦略示唆では、継続シグナルは「順張り・警戒継続」、新規シグナルは「監視・小さく試す」、消滅シグナルは「反転確認待ち」と分ける
- シグナル履歴を使う場合も、空売り比率は残高ではなく日次フローである点を維持する
"""


def build_user_prompt(
    target_date: str,
    today_summary: dict,
    weekly_df,
    anomalies: list,
    extra_news: str = "",
    auto_fetch_news: bool | None = None,
) -> str:
    """
    当日データ・週次推移・異常値を組み合わせたユーザープロンプト。
    """
    # セクターデータを整形
    sector_lines = []
    for s in today_summary.get("sector_data", []):
        dod_str = f"{s['dod_change']:+.1f}pt" if s.get("dod_change") is not None else "N/A"
        total_volume = s.get("total_volume_va", 0) or 0
        short_with = s.get("shrt_with_res_va", 0) or 0
        short_without = s.get("shrt_no_res_va", 0) or 0
        total_short = s.get("total_short_va", short_with + short_without) or 0
        with_ratio = short_with / total_volume * 100 if total_volume else 0
        without_ratio = short_without / total_volume * 100 if total_volume else 0
        without_share = short_without / total_short * 100 if total_short else 0
        sector_lines.append(
            f"{s['sector_name']:20s}: 総空売り{s['short_ratio_pct']:5.1f}% ({dod_str}) / "
            f"規制あり{with_ratio:4.1f}% / 規制なし{without_ratio:4.1f}% "
            f"(規制なし構成比{without_share:4.1f}%) / {s['zone_label']}"
        )
    sector_table = "\n".join(sector_lines)

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
    if total_volume:
        short_with = market_breakdown.get("shrt_with_res_va", 0)
        short_without = market_breakdown.get("shrt_no_res_va", 0)
        total_short = market_breakdown.get("total_short_va", short_with + short_without)
        actual = market_breakdown.get("sell_ex_short_va", 0)
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

    market_context = build_market_context_bundle(
        target_date=target_date,
        today_summary=today_summary,
        manual_news=extra_news,
        baseline_context=CURRENT_MACRO_CONTEXT,
        auto_fetch_news=(
            auto_fetch_news if auto_fetch_news is not None else MARKET_NEWS_AUTO_FETCH
        ),
    )
    theme_transition_context = build_theme_transition_context_for_prompt(
        target_date=target_date,
        today_summary=today_summary,
        current_news_text=market_context.combined_news_text,
    )

    return f"""
【分析対象日】: {target_date}

【現在の支配的マクロ背景・市場テーマ判定】:
{market_context.to_prompt_block()}

【市場テーマ履歴・転換メモ】:
{theme_transition_context}

{f'【本日の追加ニュース】:{extra_news}' if extra_news else ''}

【東証全体の空売り比率】: {today_summary.get('market_ratio', 'N/A')}%

【JPX空売り内訳】:
{breakdown_text}

【その他（33業種外）の影響】:
{other_text}

【週次推移（直近）】:
{weekly_summary if weekly_summary else '  データなし'}

【業種別データ（高い順、JPX内訳付き）】:
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
  - 未取得のVIX、WTI、SOX、GEX、米金利、ドル円などを、実測済みデータとして断定しない。

上記データを NEO真金融グランドマスター として分析し、
「空売り比率 完全解読レポート」を指定のJSONフォーマットで出力してください。
特に、価格規制あり主導なのか、価格規制なし主導なのか、その他（33業種外）が市場全体を歪めているかを必ず明記してください。
機械判定シグナルは結論の補助材料として使い、過剰に断定せず、反証条件も含めてください。
シグナル履歴は、単日ノイズと継続フローを区別するために使ってください。
出力では `signal_history_analysis`、`persistent_signal_summary`、`new_signal_summary`、`faded_signal_summary` に必ず履歴分析を記述してください。
出力では `investment_guardrails`、`confirmation_conditions`、`false_positive_risks`、`additional_data_to_check` に必ず投資判断ガードレールを記述してください。
出力では `dominant_market_themes`、`theme_shift_analysis`、`theme_sector_alignment`、`unverified_market_data` に必ず市場テーマ判定を記述してください。
"""


def build_theme_transition_context_for_prompt(
    target_date: str,
    today_summary: dict,
    current_news_text: str = "",
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
            baseline_context=CURRENT_MACRO_CONTEXT,
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
