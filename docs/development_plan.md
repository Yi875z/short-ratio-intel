# 📋 開発計画書
## 空売り比率インテリジェンス・システム
### 〜 NEO真金融グランドマスター × Gemini 3 Flash 統合分析エンジン 〜

**作成日:** 2026年4月25日
**ターゲット環境:** Cursor IDE + Claude Code
**完成予想期間:** 3週間（MVP）／ 6週間（フル機能版）

---

## 🎯 第1部: プロジェクト戦略

### 1.1 プロジェクト概要

J-Quants APIから日々の業種別空売り比率データを自動取得し、Gemini 3 Flash Preview API を介して NEO真金融グランドマスターのナレッジベースに基づく構造化レポートを自動生成する Web アプリケーション。

**コア価値提案:**
- ✅ stock-marketdata.com の手動確認を完全自動化
- ✅ 33業種ヒートマップ × ProとRetailの視点対比を毎営業日生成
- ✅ Step 0 プロトコル（過去年パターン汚染防止）を構造的に組み込み
- ✅ 現在のマクロ背景（2026年: イラン情勢）を動的に文脈に組み込み

### 1.2 ビジネス目的（Why）

```
現状の課題:
  - 日々の空売り比率をWebで目視確認 → 時間コスト大
  - 33業種を一覧で「機関の意図」付きで読むのは困難
  - 学習データの古いマクロパターンがAI解釈に混入するリスク

ToBeの状態:
  - 毎営業日 18:00 JST に自動生成されるレポート
  - 「Retail Trap vs Pro Intent」フレームで構造化
  - 今日のマクロ背景（イラン停戦延長など）を冒頭に明記
  - 売買戦略への即時反映が可能
```

### 1.3 機能要件（What）

| ID | 機能名 | 優先度 | 備考 |
|---|---|---|---|
| F-01 | J-Quants API データ自動取得 | P0 | 毎日17:00 JST、cron実行 |
| F-02 | SQLiteへのデータキャッシュ | P0 | レート制限回避 |
| F-03 | 空売り比率の計算・正規化 | P0 | 比率＝(価格規制有り+無し)÷売買代金合計 |
| F-04 | 33業種ヒートマップ生成 | P0 | 当日値・週次推移 |
| F-05 | 異常値検知（前日比 ±3pt超） | P0 | アラート対象 |
| F-06 | Gemini API による構造化レポート生成 | P0 | NEOナレッジ参照 |
| F-07 | 現在マクロ背景の動的取得 | P0 | Step 0プロトコル準拠 |
| F-08 | Webダッシュボード表示 | P0 | Streamlit |
| F-09 | レポートMD/DOCXエクスポート | P1 | 共有用 |
| F-10 | Slackアラート連携 | P1 | 異常値検知時 |
| F-11 | 過去レポート履歴閲覧 | P1 | 比較分析用 |
| F-12 | TradingViewチャート埋込 | P2 | 個別銘柄遷移 |

### 1.4 非機能要件（How well）

```
パフォーマンス:
  - データ取得+分析+レポート生成: 60秒以内
  - Gemini API呼び出し: 1日1回（コスト最適化）

可用性:
  - ローカル/クラウドどちらでも動作可能
  - API障害時は前日データでフォールバック

セキュリティ:
  - APIキーは .env で管理、git管理外
  - J-Quants認証情報も同様

コスト想定:
  - Gemini 3 Flash Preview: 約$0.05/日（input 30K + output 5K tokens想定）
  - J-Quants: 既存契約利用
  - 月次総コスト: 約$2〜3
```

---

## 🏗 第2部: システム設計

### 2.1 アーキテクチャ全体図

```
┌─────────────────────────────────────────────────────────────────┐
│                     Cron (毎営業日 17:00 JST)                    │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 1: Data Fetcher]                                        │
│  - J-Quants API認証 (refresh token)                              │
│  - /v2/markets/short-ratio?date=YYYYMMDD 取得                    │
│  - レスポンス検証・正規化                                         │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 2: Local Storage]                                       │
│  SQLite: short_ratio_daily                                       │
│    (date, s33_code, sector_name,                                 │
│     sell_ex_short_va, shrt_with_res_va, shrt_no_res_va,          │
│     short_ratio_pct, calculated_at)                              │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 3: Quant Analyzer]                                      │
│  - 33業種ランキング                                               │
│  - 前日比・週間変化計算                                           │
│  - 異常値検知 (Zスコア > 2.0 or Δ > 3pt)                         │
│  - クラスタリング (高/中/低 ゾーン分類)                          │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 4: Macro Context Fetcher] ★Step 0プロトコル              │
│  - Web検索 or Newsfeed API                                       │
│  - "日経平均 [今日の日付] 原因" 等で現在背景取得                    │
│  - 過去年パターン汚染を防止                                       │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 5: AI Report Generator] ⭐ Gemini 3 Flash Preview        │
│  System Prompt:                                                  │
│    - NEOグランドマスター人格定義                                  │
│    - 4つのナレッジファイル                                        │
│    - 出力JSONスキーマ                                            │
│  User Input:                                                     │
│    - 当日数値+ヒストリカル                                        │
│    - 現在マクロ背景                                              │
│    - 異常値リスト                                                │
│  Output:                                                         │
│    - 構造化レポート(JSON)                                        │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  [Module 6: Report Renderer]                                     │
│  - Streamlit Webダッシュボード                                   │
│  - Plotly/Recharts でビジュアル化                                │
│  - MD/DOCXエクスポート                                           │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼ (オプション)
┌─────────────────────────────────────────────────────────────────┐
│  [Module 7: Alerter]  Slack Webhook                              │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 技術スタック決定

| レイヤー | 採用技術 | 採用理由 |
|---|---|---|
| 言語 | Python 3.11+ | J-Quants Python SDK・Gemini SDK共に成熟 |
| Webフレームワーク | **Streamlit** (Phase 1) | 高速プロトタイピング・既存資産活用 |
| | Next.js + Recharts (Phase 2) | プロダクション向け |
| データ保存 | SQLite (Phase 1) | ローカル完結・依存少 |
| | Supabase (Phase 2) | クラウド/共有時 |
| AI | Gemini 3 Flash Preview | 指定モデル・コスト効率 |
| データ取得 | J-Quants API v2 | 公式・契約済み |
| ニュース取得 | Web検索 (Tavily/Brave Search API) | Step 0用マクロ背景取得 |
| スケジューラ | APScheduler | Pythonネイティブ・cronより堅牢 |
| 通知 | Slack Webhook | シンプル |
| 開発環境 | Cursor IDE + Claude Code | 既存環境 |

### 2.3 ディレクトリ構造

```
short-ratio-intelligence/
├── .env.example                    # 環境変数テンプレート
├── .gitignore
├── README.md
├── pyproject.toml                  # Poetry/uv パッケージ管理
├── requirements.txt
│
├── config/
│   ├── settings.py                 # 共通設定
│   ├── sectors.py                  # 33業種コード辞書
│   └── thresholds.py               # 異常値検知の閾値定義
│
├── src/
│   ├── __init__.py
│   │
│   ├── data_fetcher/
│   │   ├── jquants_client.py       # J-Quants API クライアント
│   │   ├── jquants_auth.py         # 認証・トークン管理
│   │   └── normalizer.py           # データ正規化
│   │
│   ├── storage/
│   │   ├── db.py                   # SQLite接続
│   │   ├── models.py               # SQLAlchemyモデル
│   │   └── migrations/             # スキーマ管理
│   │
│   ├── analyzer/
│   │   ├── ratio_calculator.py     # 比率計算
│   │   ├── anomaly_detector.py     # 異常値検知
│   │   ├── timeseries.py           # 時系列分析
│   │   └── ranker.py               # 業種ランキング
│   │
│   ├── macro_context/              # ★Step 0 プロトコル
│   │   ├── news_fetcher.py         # ニュース検索
│   │   ├── context_builder.py      # マクロ文脈組立
│   │   └── frame_router.py         # イラン/関税フレーム判定
│   │
│   ├── ai_engine/
│   │   ├── gemini_client.py        # Gemini API クライアント
│   │   ├── prompt_builder.py       # プロンプト動的構築
│   │   ├── system_prompts.py       # NEO人格定義
│   │   └── output_schema.py        # JSON出力スキーマ (Pydantic)
│   │
│   ├── knowledge/                  # ⭐ NEOグランドマスター
│   │   ├── __init__.py
│   │   ├── loader.py               # MDファイル読込
│   │   └── files/
│   │       ├── 01_global_macro.md  # プロジェクトファイルから移植
│   │       ├── 02_jpx_micro.md
│   │       ├── 03_options_gex.md
│   │       └── 04_quant_psych.md
│   │
│   ├── reporter/
│   │   ├── markdown_renderer.py
│   │   ├── docx_renderer.py
│   │   └── slack_alerter.py
│   │
│   └── scheduler/
│       └── daily_job.py            # APScheduler ジョブ定義
│
├── app/                            # Streamlit Webアプリ
│   ├── streamlit_app.py            # エントリポイント
│   ├── pages/
│   │   ├── 01_today_dashboard.py
│   │   ├── 02_sector_drill_down.py
│   │   ├── 03_history.py
│   │   └── 04_settings.py
│   └── components/
│       ├── heatmap.py
│       ├── trend_chart.py
│       └── ai_report_view.py
│
├── tests/
│   ├── test_jquants_client.py
│   ├── test_analyzer.py
│   ├── test_gemini_integration.py
│   └── fixtures/
│       └── sample_response.json
│
└── data/                           # gitignore
    ├── short_ratio.db              # SQLite DB
    └── reports/
        └── YYYY-MM-DD.md           # 生成レポート保存
```

---

## 🔌 第3部: API統合詳細

### 3.1 J-Quants API 統合

**認証フロー:**
```python
# src/data_fetcher/jquants_auth.py の概要

# Step 1: refresh_token取得（手動・初回のみ）
POST https://api.jquants.com/v1/token/auth_user
Body: {"mailaddress": "...", "password": "..."}
→ refreshToken (有効期限7日)

# Step 2: idToken取得（24時間ごと自動更新）
POST https://api.jquants.com/v1/token/auth_refresh?refreshtoken=...
→ idToken (有効期限24時間)

# Step 3: APIコール時にidToken使用
GET /v2/markets/short-ratio?date=20260424
Header: Authorization: Bearer {idToken}
```

**取得・正規化ロジック:**
```python
# 例: 当日全業種データの取得
{
  "date": "2026-04-24",
  "s33": "0050",  # 水産・農林業
  "SellExShortVa": 1333126400.0,
  "ShrtWithResVa": 787355200.0,
  "ShrtNoResVa": 149084300.0
}

# 計算ロジック:
total_short = ShrtWithResVa + ShrtNoResVa
total_volume = SellExShortVa + total_short  # 売買代金合計
short_ratio_pct = (total_short / total_volume) * 100
```

**33業種コード辞書 (config/sectors.py):**
```python
SECTORS_S33 = {
    "0050": "水産・農林業",
    "1050": "鉱業",
    "2050": "建設業",
    "3050": "食料品",
    "3100": "繊維製品",
    # ... 全33業種
    "9050": "サービス業",
}
```

### 3.2 Gemini 3 Flash Preview 統合

**システムプロンプト構造:**
```python
SYSTEM_PROMPT = f"""
あなたは「NEO真 金融グランドマスター 👑 The Omni-Market Sovereign」です。
日米市場の精緻なフロー分析と機関投資家視点の戦略立案が役目です。

【重要・最優先】Step 0 プロトコル:
1. 数値解釈の前に必ず以下を確認:
   - 現在の支配的マクロ背景（提供される `current_macro_context` を採用）
   - 学習データの記憶だけで原因断定することは禁止
2. 出力冒頭に「現在の支配的マクロ背景: ○○」を1行で明記する。

【ナレッジベース】
以下4つのドキュメントの内容を完全に体得してください:

--- Global Macro Dynamics ---
{load_md('01_global_macro.md')}

--- JPX Micro Flows ---
{load_md('02_jpx_micro.md')}

--- Options & GEX Master ---
{load_md('03_options_gex.md')}

--- Quant & Psychology ---
{load_md('04_quant_psych.md')}

【出力フォーマット】
必ず以下のJSON Schemaに従って出力してください:
{OUTPUT_JSON_SCHEMA}

【分析の鉄則】
- Retail Trap vs Pro Intent の対比を必ず含める
- 業種別解釈には「テーマ売り」の文脈を明記
- 異常値（Zスコア±2超）には特別な注釈を付与
- 過去年の類似イベントの安易な投影は禁止
"""
```

**ユーザープロンプト構築:**
```python
USER_PROMPT_TEMPLATE = """
【分析対象日】: {target_date}
【現在の支配的マクロ背景】: {current_macro_context}

【当日の東証全体データ】
- 空売り比率: {today_ratio}%
- 前日比: {dod_change}pt
- 過去5営業日推移: {weekly_trend}
- 売買代金: {total_volume}億円

【33業種別データ】
{sector_table_csv}

【検出された異常値】
{anomalies_list}

【週間サマリー】
- 週間最高: {weekly_max} ({weekly_max_date})
- 週間最低: {weekly_min} ({weekly_min_date})
- 週間平均: {weekly_avg}

上記データを、NEOグランドマスターとして
「空売り比率 完全解読レポート」形式で構造化分析してください。
"""
```

**出力JSONスキーマ (Pydantic):**
```python
from pydantic import BaseModel
from typing import List

class SectorAnalysis(BaseModel):
    sector_name: str
    s33_code: str
    short_ratio_pct: float
    deviation_from_avg: float
    zone: str  # "high_alert" | "watch" | "normal" | "low_squeeze_candidate"
    interpretation: str
    pro_intent: str

class StrategicSuggestion(BaseModel):
    suggestion_id: int
    title: str
    target_sectors: List[str]
    strategy_type: str  # "long" | "short" | "hedge" | "options"
    risk_level: str
    rationale: str

class ReadingReport(BaseModel):
    target_date: str
    current_macro_context: str  # ★Step 0必須項目
    market_overall_summary: str
    weekly_trend_analysis: str
    sector_analyses: List[SectorAnalysis]
    retail_trap: str
    pro_intent: str
    strategic_suggestions: List[StrategicSuggestion]
    overall_conclusion: str
```

### 3.3 マクロ文脈取得ロジック (Step 0 プロトコル実装)

```python
# src/macro_context/context_builder.py

class MacroContextBuilder:
    """
    現在の支配的マクロ背景を動的に取得・構築する。
    過去年パターン汚染を構造的に防止。
    """

    # ベースとなる確定済みコンテキスト（手動更新）
    CURRENT_BASELINE_CONTEXT = """
    主要マクロ背景（2026年2月28日〜継続中）:
    米国・イスラエル vs イランの軍事紛争

    影響チェーン:
    1. ホルムズ海峡リスク → 原油供給不安 → WTI高止まり
    2. 原油高 → インフレ再燃懸念 → FRBタカ派化圧力
    3. スタグフレーション懸念 → リスクオフ → 日本株下押し
    """

    def build(self, target_date: str) -> str:
        # ① ベースラインを採用
        context = self.CURRENT_BASELINE_CONTEXT

        # ② 当日の追加ニュースを検索
        news = self.fetch_today_news(target_date)
        context += f"\n\n【{target_date}時点の追加トピック】\n{news}"

        return context

    def fetch_today_news(self, date: str) -> str:
        # Tavily/Brave Search API でニュース検索
        # キーワード: "日経平均 [date] 原因", "WTI イラン", "BOJ", etc.
        ...
```

---

## 🧠 第4部: 分析ロジック詳細

### 4.1 異常値検知アルゴリズム

```python
# src/analyzer/anomaly_detector.py

class AnomalyDetector:
    THRESHOLDS = {
        "dod_change_alert": 3.0,      # 前日比±3pt超でアラート
        "zscore_alert": 2.0,           # 過去30営業日のZスコア±2超
        "absolute_high": 50.0,         # 絶対値50%超で警戒
        "absolute_low": 30.0,          # 絶対値30%未満で楽観警戒
    }

    def detect(self, today_data, history_30d):
        anomalies = []

        # ① 前日比急変
        for sector in today_data:
            dod = sector.ratio - sector.prev_ratio
            if abs(dod) >= self.THRESHOLDS["dod_change_alert"]:
                anomalies.append(AnomalyEvent(
                    type="dod_spike",
                    sector=sector.name,
                    value=dod,
                    severity="high" if abs(dod) >= 5 else "medium"
                ))

        # ② Zスコア検知（過去30日からの逸脱）
        for sector in today_data:
            mean = history_30d[sector.code].mean()
            std = history_30d[sector.code].std()
            z = (sector.ratio - mean) / std
            if abs(z) >= self.THRESHOLDS["zscore_alert"]:
                anomalies.append(AnomalyEvent(
                    type="zscore_outlier",
                    sector=sector.name,
                    zscore=z
                ))

        # ③ 絶対値ゾーン
        # ...

        return anomalies
```

### 4.2 業種ランキング・ゾーン分類

```python
# src/analyzer/ranker.py

ZONES = {
    "extreme_short": {"min": 50.0, "color": "#c0392b",
                      "label": "🔴 極端な売り集中"},
    "high_alert":    {"min": 47.0, "color": "#e67e22",
                      "label": "🟠 警戒ゾーン"},
    "elevated":      {"min": 43.0, "color": "#f39c12",
                      "label": "🟡 やや高め"},
    "normal":        {"min": 37.0, "color": "#3498db",
                      "label": "🔵 正常レンジ"},
    "low":           {"min": 30.0, "color": "#16a085",
                      "label": "🟢 低空売り"},
    "extreme_low":   {"min":  0.0, "color": "#27ae60",
                      "label": "🟢🟢 極端な低水準（流動性注意）"},
}
```

---

## 🎨 第5部: UI/UX 設計

### 5.1 Streamlit ダッシュボード構成

**ページ1: Today Dashboard（メイン画面）**
```
┌─────────────────────────────────────────────────────┐
│  📊 空売り比率インテリジェンス                         │
│  ───────────────────────────────────                │
│  [日付選択: 2026-04-24] [更新ボタン]                  │
├─────────────────────────────────────────────────────┤
│                                                       │
│  KPI カード (4枚並び)                                 │
│  ┌────┐ ┌────┐ ┌────┐ ┌────┐                       │
│  │40.4│ │41.8│ │36.6│ │+3.8│                        │
│  │直近│ │週高│ │週低│ │金変│                        │
│  └────┘ └────┘ └────┘ └────┘                       │
│                                                       │
│  [推移ライン] | [業種棒グラフ TOP15]                  │
│                                                       │
│  [33業種ヒートマップ]                                 │
│                                                       │
│  ⭐ AIレポート (Markdown表示)                         │
│  - 現在の支配的マクロ背景                            │
│  - シナリオ分析                                      │
│  - Retail Trap vs Pro Intent                         │
│  - 戦略的示唆                                        │
│                                                       │
│  [📥 MDダウンロード] [📥 DOCXダウンロード]            │
└─────────────────────────────────────────────────────┘
```

**ページ2: Sector Drill-down**
- 業種選択 → 過去90日推移 + 個別解釈

**ページ3: History**
- 過去レポート一覧・比較表示

**ページ4: Settings**
- API キー管理
- 閾値カスタマイズ
- スケジュール設定

---

## 📅 第6部: 開発フェーズ計画

### Phase 1: MVP（3週間）

**Week 1: データ基盤構築**
| Day | タスク | 担当 | 成果物 |
|---|---|---|---|
| 1 | プロジェクト初期化・依存関係設定 | Claude Code | `pyproject.toml`, `.env.example` |
| 2 | J-Quants認証・APIクライアント実装 | Claude Code | `jquants_client.py` |
| 3 | SQLite スキーマ・モデル定義 | Claude Code | `models.py`, migrations |
| 4 | データ取得・保存パイプライン | Claude Code | `daily_job.py` (cron無し版) |
| 5 | 33業種辞書・マスタ整備 | Claude Code | `config/sectors.py` |
| 6-7 | テスト・履歴データ初期投入 | あなた | 過去90日分のデータ |

**Week 2: AI分析エンジン構築**
| Day | タスク | 成果物 |
|---|---|---|
| 8 | NEOナレッジMDファイル整備 | `knowledge/files/*.md` |
| 9 | Gemini APIクライアント・認証 | `gemini_client.py` |
| 10 | システムプロンプト構築 | `system_prompts.py` |
| 11 | Pydantic出力スキーマ定義 | `output_schema.py` |
| 12 | プロンプト動的構築ロジック | `prompt_builder.py` |
| 13 | マクロ文脈取得ロジック (Step 0) | `context_builder.py` |
| 14 | 異常値検知・ランキング実装 | `anomaly_detector.py`, `ranker.py` |

**Week 3: UI構築・統合**
| Day | タスク | 成果物 |
|---|---|---|
| 15 | Streamlit エントリ・ルーティング | `streamlit_app.py` |
| 16 | KPIカード・ヒートマップ | `components/heatmap.py` |
| 17 | トレンドチャート (Plotly) | `components/trend_chart.py` |
| 18 | AIレポート表示コンポーネント | `components/ai_report_view.py` |
| 19 | MD/DOCXエクスポート | `markdown_renderer.py` |
| 20 | E2Eテスト・バグ修正 | テストレポート |
| 21 | デプロイ・初回本番実行 | 稼働中システム |

### Phase 2: フル機能（追加3週間）

```
Week 4: スケジューラ・自動化
  - APScheduler導入、毎日17:00 JST自動実行
  - Slack Webhookアラート
  - エラー時のフォールバック・リトライ

Week 5: 拡張機能
  - 過去レポート履歴ページ
  - 業種ドリルダウンページ
  - 個別銘柄レベルのスクリーニング (オプション)

Week 6: プロダクション化
  - Supabaseへの移行 (オプション)
  - Next.jsフロントエンド (オプション)
  - Dockerization
  - CI/CD (GitHub Actions)
```

---

## 🛡 第7部: リスクと対策

| リスク | 確率 | 影響 | 対策 |
|---|---|---|---|
| J-Quants API障害 | 中 | 高 | キャッシュ参照・前日データでフォールバック |
| Gemini APIレート制限 | 低 | 中 | リトライロジック、バックオフ |
| Step 0プロトコル劣化 | 中 | 高 | マクロ背景を毎回ログに記録、週次レビュー |
| プロンプトインジェクション | 低 | 中 | ナレッジ参照は固定MDのみ、外部入力は検証 |
| コスト爆増 | 低 | 中 | 月次コストモニタリング、$10超でアラート |
| 学習データ汚染 (過去年混入) | 中 | 高 | output_schemaで`current_macro_context`必須化 |

---

## 🚀 第8部: Claude Code向け実装指示テンプレート

Cursor/Claude Code に最初に投げるプロンプト例:

```
このプロジェクト `short-ratio-intelligence` を以下の要件で実装してください。

【参照ドキュメント】
- /docs/development_plan.md (本ドキュメント)

【今回のタスク: Phase 1 Week 1 Day 1-2】
- pyproject.toml と requirements.txt を作成
  依存: requests, pydantic, sqlalchemy, streamlit,
        plotly, pandas, google-genai, python-dotenv,
        apscheduler
- /src/data_fetcher/jquants_auth.py を実装
  仕様: 開発計画書 §3.1 の認証フロー
  - .env から JQUANTS_REFRESH_TOKEN を読み込み
  - idTokenを24時間キャッシュ (ローカルファイル)
  - 期限切れ時は自動更新
- /src/data_fetcher/jquants_client.py を実装
  仕様: 開発計画書 §3.1 のAPIコール
  - get_short_ratio(date: str) -> List[Dict]
  - get_short_ratio_range(from_date: str, to_date: str)
  - エラーハンドリング (リトライ3回、指数バックオフ)
- pytest でテスト作成
  - モックレスポンスでの成功ケース
  - 401エラー時のidToken再取得
  - 429レート制限時のバックオフ

【コーディング規約】
- 型ヒント必須 (Python 3.11+)
- docstring必須 (Google style)
- ロギングは loguru を使用
- 設定は config/settings.py から読込
```

---

## 📌 第9部: 実装チェックリスト

### MVP完成判定

```
□ J-Quants API から当日全業種データが取得できる
□ SQLiteに永続化されている
□ Gemini API でレポートが生成される
□ レポートに「現在の支配的マクロ背景」行が含まれる
□ Retail Trap vs Pro Intent が必ず対比されている
□ Streamlitで33業種ヒートマップが表示される
□ MD/DOCXエクスポートが動作する
□ 過去5営業日推移が可視化されている
□ 異常値（前日比±3pt超）が検知・表示される
```

### Phase 2完成判定

```
□ 毎営業日17:00 JST に自動実行される
□ 異常値検知時にSlackへ通知される
□ 過去レポート履歴ページから差分比較できる
□ 業種ドリルダウンから過去90日チャートが見られる
□ デプロイ済み (Streamlit Cloud or VPS)
```

---

## 📎 付録A: コスト見積もり

```
月次運用コスト試算:

Gemini 3 Flash Preview:
  - Input: 30,000 tokens/日 × $0.50/1M = $0.015/日
  - Output: 5,000 tokens/日 × $3.00/1M = $0.015/日
  - 営業日換算: ($0.015 + $0.015) × 20日 ≒ $0.60/月

J-Quants:
  - 既存契約利用 (追加コスト$0)

Web検索 API (Tavily Free Tier):
  - 1日10回程度 → 無料枠内 ($0)

ホスティング (Streamlit Cloud Free):
  - $0 (Phase 1)

合計: 約$0.60〜$2.00/月 (Phase 1 MVP)
```

## 📎 付録B: 関連スキル・ナレッジ統合方針

```
本プロジェクトで活用する既存スキル・知識:

⭐ omni-market-agent v4 (確認事項6プロトコル準拠)
  → AIプロンプト設計の核
  → Step 0 過去年パターン汚染防止を実装

⭐ NEO真金融グランドマスター ナレッジ (4ファイル)
  → src/knowledge/files/ に配置
  → Geminiのシステムプロンプトに展開

⭐ jpx-investor-data v2 (補完用)
  → Phase 2で投資主体別フローと連携した拡張分析を追加可能

⭐ 既存の空売り残高追跡システム (Cursor + Claude Code開発中)
  → コードベース・SQLite設計を参考に流用可
```

---

**END OF DEVELOPMENT PLAN**

このドキュメントは Claude Code でそのまま Cursor のコンテキストに投入し、Phase 1 Week 1 Day 1 のタスクから順次実装可能な粒度で記述されている。
