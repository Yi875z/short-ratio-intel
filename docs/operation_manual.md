# 空売り比率インテリジェンス 運用マニュアル

最終更新: 2026-05-30

---

## システム概要

JPXの空売り集計データと業種別空売り比率を取得・分析し、現在の市場テーマを踏まえた Gemini AI レポートを生成する Streamlit アプリ。**2026-05-30 にクラウド化が完了**し、PCに依存せず動く本番環境と、従来どおりの手元開発環境の2モードで動作する。

- **データソース**: JPX公式空売り集計PDF、stock-marketdata.com（フォールバック）。いずれも公開スクレイピングで、認証キーは不要。
- **AI**: Gemini API。既定モデルは `gemini-3.6-flash`（`GEMINI_MODEL` で変更可。2026-07-25 に 3.5→3.6。2026-08-23 に 3.7 へ移行したが 08-24 の本番障害で差し戻し）
- **任意ニュース取得**: Tavily API（市場テーマ判定・AIレポート生成時）
- **データ保存先**: 環境変数 `DATABASE_URL` があれば Supabase(PostgreSQL)、無ければローカル SQLite に自動で切り替わる（`src/storage/db.py` の `get_engine()`）

### 2モードの違い

| | クラウド（本番） | ローカル（開発） |
|---|---|---|
| 閲覧 | スマホ/PCブラウザ（Streamlit Cloud） | `localhost:8501` |
| データ取得 | GitHub Actions が平日自動 | `アプリ起動.bat` から手動ボタン |
| DB | Supabase(PostgreSQL) | ローカル SQLite（`DATABASE_URL` 未設定時） |
| ログイン | bcrypt 認証あり | 認証なし |

---

## クラウド構成（本番・PC非依存）

```
GitHub Actions（平日19:00 JST）           Streamlit Community Cloud（常時稼働）
  └ scripts/fetch_short_ratio.py            └ app/streamlit_app.py
       取得 → 市場テーマ判定 → AIレポート        Supabaseを読んでスマホ表示
       │                                        │
       └──────────►  Supabase(PostgreSQL)  ◄────┘
                     （市場データ・AIレポート・ナレッジを一元保存）
```

- **公開URL**: Streamlit Cloud の `*.streamlit.app`（ブックマーク可）。開くと bcrypt ログイン画面が出る。ユーザー名・パスワードは作成者が保持（このリポジトリは公開のため、認証情報は記載しない）。
- **GitHubリポジトリ**: `Yi875z/short-ratio-intel`（Public。Streamlit Cloud 無料プランの要件）
- **Supabase**: Tokyo(ap-northeast-1) / nano。SQLAlchemy が Session pooler 接続文字列（`DATABASE_URL`）で直接続。REST API キーは使わない。
- **秘密情報**: `DATABASE_URL` / `GEMINI_API_KEY` / `TAVILY_API_KEY` は GitHub Secrets と Streamlit Cloud Secrets に登録（リポジトリには含めない）。`.env` と `.streamlit/secrets.toml` は `.gitignore` 済み。

### 自動取得（GitHub Actions）

- 定義: `.github/workflows/daily_fetch.yml`。cron `0 10 * * 1-5`（= 平日19:00 JST）。
- 手動実行: GitHub の Actions タブ →「空売り比率 定時取得・AIレポート生成」→ Run workflow。モードは `full`（取得＋テーマ＋レポート）/ `fetch-only` / `no-news` を選べる。スマホからも実行可。
- **⚠️ `gh workflow run` をループ/自動リトライで叩かないこと。** 一時的な500でも単発・手動で1回ずつ。過去に別プロジェクトでループ誤記により本番ワークフローが連続実行・大量メールの事故あり。

### スタンドアロン取得スクリプト

`scripts/fetch_short_ratio.py` は Streamlit に依存せず、取得→市場テーマ判定→AIレポート生成→Slack通知（任意）を一気通貫で行う。GitHub Actions もこれを呼ぶ。手元でも実行可：

```powershell
python -m scripts.fetch_short_ratio                 # 直近5営業日＋最新日でフル処理
python -m scripts.fetch_short_ratio --no-news       # Tavilyを使わない
python -m scripts.fetch_short_ratio --date 2026-05-28  # 特定日のみ
```

---

## ローカル開発・閲覧

`DATABASE_URL` を設定しなければ従来どおりローカル SQLite で動く。

### ワンクリック起動（推奨）

`C:\CarSol\short-ratio-intel\アプリ起動.bat` をダブルクリック。ポート競合は自動解消、ブラウザが http://localhost:8501 を開く。**黒いウィンドウを閉じるとアプリも停止する。**

### コマンドで起動

```powershell
python -m streamlit run app\streamlit_app.py
```

`.env` に `DATABASE_URL` を書くと、ローカルからでも Supabase に接続して本番と同じデータを見られる（書き込みも本番に反映されるので注意）。

---

## 外部ナレッジ（思考データ）の更新

レポートの思考の土台になる外部ナレッジは、公開リポジトリにファイルを置かず **Supabase の `knowledge_documents` テーブルに保存**している。原本は `C:\CarSol\knowledgefile\*.md`。

ナレッジを更新したら、原本を編集して以下を実行すればクラウド（Streamlit Cloud / GitHub Actions）にも反映される：

```powershell
python -m scripts.upload_knowledge_to_supabase          # 全ファイルをupsert
python -m scripts.upload_knowledge_to_supabase --list   # 登録済みkey確認
```

読み込みは `src/knowledge/loader.py` が **Supabase優先 → 無ければローカルファイル**の順で行う。

---

## データ取得見込みチェック

左メニューの取得日を指定し「取得見込みチェック」→「指定日の取得可否を確認」で、DB保存前に公開元の状態を確認できる。判定が「取得可能」なら「指定日を取得」で保存できる見込みが高い。「一部取得可能」「未公開または取得不可」のときは公開待ち・非営業日・通信制限・公開元サイト変更の可能性があるため時間を置いて再確認する。

---

## AIレポート品質チェック

AIレポートタブでは、保存済みレポート上部に「AIレポート品質チェック」が表示される。必須セクションの有無、安全表現の有無、過剰断定の有無、未確認データ（VIX/WTI/SOX/GEX/米金利/ドル円）の断定有無、構造化JSONの保存状況、テーマ履歴の反映などを判定する。

「要修正/要確認」のときは表示行の `message`・`evidence` を確認し、市場メモを補足して再生成する。同じ日付を再生成すると、前回の失敗項目が「改善指示」として自動でプロンプトに渡る（再生成前にプレビュー可、チェックを外せば使わない）。品質チェックはCSV/Markdownでダウンロードでき、再生成直後は前後スコアの差分（品質比較）を確認できる。品質比較は `data/reports/ai_report_quality_comparison_YYYY-MM-DD.md` に保存される。履歴タブでは日付別スコアを一覧でき、再生成の優先順位付けに使える。

---

## 市場テーマ履歴

市場テーマタブで保存済みテーマ判定の履歴を確認できる（前回保存日とのスコア比較、新規/強化/継続/弱体化/消滅の分類、直近30件のスコア推移、関連業種・根拠数）。テーマが変わったら先にテーマ判定を保存し、その後 AIレポートを生成する。AIレポート生成時にはこの履歴から「市場テーマ履歴・転換メモ」が自動でプロンプトへ挿入される。

---

## ファイル構成（重要箇所のみ）

```
C:\CarSol\short-ratio-intel\
├── アプリ起動.bat                       ← ローカルのワンクリック起動
├── DEPLOY.md                            ← クラウド構築手順（再現用）
├── .github\workflows\daily_fetch.yml    ← 定時取得ワークフロー
├── .streamlit\secrets.toml.example      ← Streamlit Cloud Secrets のテンプレ
├── app\streamlit_app.py                 ← アプリ本体（先頭でbcryptログイン）
├── src\
│   ├── data_fetcher\                    ← JPX PDF / stock-marketdata スクレイパー
│   ├── ai_engine\gemini_client.py       ← Geminiレポート生成（JSON修復つき）
│   ├── knowledge\loader.py              ← ナレッジ読込（Supabase優先）
│   └── storage\db.py                    ← DB接続（DATABASE_URLで切替）
├── scripts\
│   ├── fetch_short_ratio.py             ← 定時パイプライン本体
│   ├── migrate_sqlite_to_supabase.py    ← SQLite→Supabase移行（初回のみ）
│   └── upload_knowledge_to_supabase.py  ← ナレッジをSupabaseへ
├── config\settings.py                   ← 設定（環境変数を読む）
├── data\                                ← ローカルSQLite等（Gitで除外）
└── .env                                 ← APIキー・DATABASE_URL（Gitで除外）
```

---

## 重要な注意事項

- **秘密情報をコミットしない**: `.env`・`.streamlit/secrets.toml`・`*.db` は `.gitignore` 済み。リポジトリは公開なので、APIキー・接続文字列・ログインパスワードを追跡ファイルに書かない。
- **DBの切替**: `DATABASE_URL` があれば Supabase、無ければ SQLite。ローカルで本番DBを触りたくないときは `.env` の `DATABASE_URL` をコメントアウトする。
- **`streamlit_app_original.cpython-311.pyc`**: 2026-05-07 の文字コード破損からの旧復旧用バックアップ。現在の `streamlit_app.py` は通常ソースとして保守する（2026-05-19 復元済み）。通常起動では使わない。

---

## トラブルシューティング

### Streamlit Cloud のデプロイがビルドで失敗する（pandas/numpy をソースから build しようとする）

原因は **Streamlit Cloud が新しい Python（例: 3.14）を既定にしており、固定した古い numpy/pandas に該当 wheel が無い**こと。対処は **Manage app → Settings → Python version を 3.12 に設定して保存**（依存は固定のまま wheel が入り、ビルドが成功する）。デプロイ時の Advanced settings でも指定できる。

### AIレポートで「Geminiの出力がJSON形式ではありません」エラー

Gemini は大きなJSONを途中で切ったり、文字列内に未エスケープの引用符・改行を混ぜた壊れたJSONをときどき返す。対策は実装済み：`gemini_client.py` で `max_output_tokens=32768`（切り詰め防止）＋ `json-repair` による機械修復のフォールバック。再発する場合はモデルの応答が極端に長い可能性があるため、市場メモを簡潔にして再生成する。

### Supabase に接続できない / `Invalid API key`

SQLAlchemy は **Session pooler の接続文字列**（`postgresql://...pooler.supabase.com:5432/postgres`）で直接続する。REST API キー（`sb_secret_`）は使わない。`DATABASE_URL` のパスワード部分に記号が含まれる場合は URLエンコードが必要（例 `@`→`%40`）。

### ポート8501がすでに使用中（ローカル）

`アプリ起動.bat` が自動解消する。手動なら：

```powershell
netstat -aon | findstr ":8501"
taskkill /PID <該当PID> /F
```

### アプリは起動するが「データなし」と表示される

データ取得ボタンで取得するか、接続先DB（SQLite/Supabase）にデータがあるか確認する。`DATABASE_URL` の設定有無で見ているDBが変わる点に注意。

### Geminiのクォータ（429 / limit）

Flash 系は無料枠が1日20リクエスト（20 RPD）。レポート再生成を何度も回すと枯渇しうる（1回の生成で最大3回リトライ＝3コール）。

この 20 RPD は `GenerateRequestsPerDayPerProjectPerModel-FreeTier`、つまり **モデル単位** の枠。
枯渇しても別モデルは無傷の別枠を持つので、**24時間待つ必要はなく別モデルへ移れば即復旧**する。
`gemini_client.py` は日次枠の 429 を検知したら待たずに `GEMINI_FALLBACK_MODELS`
（既定 `gemini-3.6-flash,gemini-3.5-flash`）へ自動で切り替えるので、通常は手作業不要。
恒久的に既定を変える場合のみ `GEMINI_MODEL` を書き換える。
クォータのリセットは太平洋時間の深夜＝**JST 16:00 が日付境界**。

#### モデル指定の正本は `config/settings.py` の1箇所だけ

`GEMINI_MODEL_DEFAULT`（`config/settings.py`）が唯一の正。
`daily_fetch.yml` には**あえて `GEMINI_MODEL` を置いていない**（二重管理だと
「既定だけ直したのに本番が変わらない」事故になるため。2026-08-25 に削除）。

環境変数 `GEMINI_MODEL` による上書きは緊急避難用に残してあるが、上書きが効いていると
起動ログに警告が出て、Streamlit の生成ボタン上にも `⚠️ 使用モデル: ...` と表示される。
8/24 の障害では Streamlit Cloud Secrets だけ 3.5 が残っていて手動生成は通ってしまい、
定時実行だけ落ちる食い違いの発見が遅れた。**Secrets 側に `GEMINI_MODEL` を置かない**のが既定運用。

DB の `ai_reports.model_used` には設定値ではなく**実際に使われたモデル**が入る
（自動退避が起きた日を後から追えるようにするため）。

#### 新しいモデルを採用してよいかの判定

```bash
python -m scripts.check_gemini_model gemini-3.7-flash      # 候補モデルを本番同等の入力で1回検証
```

短いプロンプトの疎通確認では判断できない（3.7 は短文なら3秒で返るが、本番入力では600秒超）。
このスクリプトは DB の実データから本番と同じ user prompt を組み立て、所要時間・生JSON長・
スキーマ検証（`_parse_response`）の可否を出す。**スキーマ検証を通り、かつ所要時間が
タイムアウトの半分以下**であることを採用条件とする。1モデルにつき無料枠を1消費する。

実測（2026-08-25 / 対象日 2026-08-24 / system 67,817字 + user 16,489字）:

| モデル | 結果 | 所要 | 備考 |
|---|---|---|---|
| `gemini-3.6-flash` | OK | 61.7秒 | 生JSON 13,319字・スキーマ検証通過。タイムアウト300秒の半分に十分収まる |
| `gemini-3.7-flash` | NG | — | 503 high demand。8/14 は504連発、8/24 は本番で600秒超 |

**3.7-flash は不採用で確定**。3度別々の壊れ方（504連発 / 600秒超 / 503高需要）をしており、
速い日があっても本番の定時実行に賭けられる安定性がない。再挑戦する場合も、
上のスクリプトを数日にわたって回して**ばらつきごと**見てから判断すること。

#### 2026-08-24 の障害（AIレポート欠落）— 遅いモデルは静かに枠を溶かす

`gemini-3.7-flash` は単発検証では 85.5秒で通ったが、本番負荷（system 67.8K字 /
`response_mime_type=application/json` / `max_output_tokens=32768`）では
google-api-core の既定デッドライン **600秒** を超えて 504 になった。
問題はここからで、SDK は 429/504 を「一時エラー」とみなし、その600秒のあいだ内部で
リクエストを投げ直す。**内部リトライ1回ごとに RPD を1消費する**ため、
10分走った1回の呼び出しだけで 20 req/日を使い切り、14分後の定時実行（19:50）が
429 で全滅して AIレポートが生成されなかった。

対策として `generate_content` に `request_options={"retry": None, "timeout": GEMINI_REQUEST_TIMEOUT_SEC}`
を渡し、**1呼び出し = 1リクエスト**を保証している。ログの `attempt N` の回数が
そのまま消費数になるので、枠の減りが読めるようになっている。
新しいモデルを採用するときは、速度を単発では判断せず、本番同等の入力で計測すること。

---

## クラウドを最初から構築し直す手順

`DEPLOY.md` に6ステップでまとめてある（Supabase作成 → データ移行 → Public push → GitHub Secrets → ワークフロー確認 → Streamlit Cloud デプロイ）。再構築・別環境への移設時はそちらを参照する。

---

## バックアップ・Git運用

Git管理下にあり、GitHub（`Yi875z/short-ratio-intel`）へ push 済み。コード変更後は：

```powershell
git add -A
git commit -m "変更内容の説明"
git push origin main
```

main へ push すると Streamlit Cloud が自動で再デプロイする。秘密情報を誤って追跡対象に含めていないか、`git status` で確認してから push すること。
