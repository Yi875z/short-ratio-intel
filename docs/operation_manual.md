# 空売り比率インテリジェンス 運用マニュアル

最終更新: 2026-05-30

---

## システム概要

JPXの空売り集計データと業種別空売り比率を取得・分析し、現在の市場テーマを踏まえた Gemini AI レポートを生成する Streamlit アプリ。**2026-05-30 にクラウド化が完了**し、PCに依存せず動く本番環境と、従来どおりの手元開発環境の2モードで動作する。

- **データソース**: JPX公式空売り集計PDF、stock-marketdata.com（フォールバック）。いずれも公開スクレイピングで、認証キーは不要。
- **AI**: Gemini API。既定モデルは `gemini-3.5-flash`（`GEMINI_MODEL` で変更可）
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

`gemini-3.5-flash` は無料枠が1日20リクエスト（20 RPD）。レポート再生成を何度も回すと枯渇しうる（1回の生成で最大3回リトライ＝3コール）。枯渇時は同じモデルを叩き直さず24時間待つ。

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
