# デプロイ手順書 — クラウド化（GitHub Actions + Supabase + Streamlit Cloud）

短売り比率インテリジェンスを **PC非依存で定時取得 ＋ スマホからWeb閲覧** にするための手順。
コード側（フェーズ1〜5）は実装済み。本書はアカウント操作が必要なフェーズ4のチェックリスト。

構成:

```
GitHub Actions (平日19:00 JST)         Streamlit Community Cloud (常時)
   └─ scripts/fetch_short_ratio.py        └─ app/streamlit_app.py
        取得→テーマ→AIレポート                 Supabaseを読んでスマホ表示
        │                                      │
        └──────────► Supabase (PostgreSQL) ◄───┘
```

DB接続は環境変数 `DATABASE_URL` で決まる。設定あり=Supabase、なし=ローカルSQLite（開発用）。

---

## ステップ1: Supabase プロジェクト作成 & 接続文字列取得

1. https://supabase.com でプロジェクトを新規作成（リージョンは Tokyo 推奨）
2. 作成時の **Database Password** を控える
3. Project Settings → **Database** → Connection string → **Session pooler** タブの URI をコピー
   - 形式: `postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres`
   - `[YOUR-PASSWORD]` を実際のパスワードに置換する
   - ※ REST API キー（`sb_secret_` / anon key）は今回**使わない**。SQLAlchemyはこのPostgres URIで直接つなぐ

テーブルは初回接続時に `Base.metadata.create_all()` が自動生成するので、手動SQLは不要。

---

## ステップ2: 既存データの移行（ローカル SQLite → Supabase）

手元の PowerShell で:

```powershell
$env:DATABASE_URL = "postgresql://postgres.xxxx:[PASSWORD]@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
python -m scripts.migrate_sqlite_to_supabase --dry-run   # 件数だけ確認
python -m scripts.migrate_sqlite_to_supabase             # 本実行
```

`--dry-run` で移行元の件数が表示される。本実行後 "移行完了: 合計 N 件" が出ればOK。
（宛先に既にデータがあるテーブルはスキップされる。やり直す場合は Supabase 側で該当テーブルを空にする）

---

## ステップ3: GitHub に Public リポジトリを作成して push

Streamlit Cloud Free は **Public リポジトリ必須**。`.env` / `*.db` / `data/` / `.streamlit/secrets.toml` は
`.gitignore` 済みなので、APIキー・DB・個人データは公開されない。

```powershell
gh repo create short-ratio-intel --public --source=. --remote=origin --push
# 既にリモートがある場合: git push -u origin main
```

---

## ステップ4: GitHub Secrets を登録

各シークレットは **1行ずつ単独実行**して対話モードで貼り付ける（複数行スクリプトで一気に貼らない）。

```powershell
gh secret set DATABASE_URL -R <OWNER>/short-ratio-intel
gh secret set GEMINI_API_KEY -R <OWNER>/short-ratio-intel
gh secret set TAVILY_API_KEY -R <OWNER>/short-ratio-intel
# 任意
gh secret set SLACK_WEBHOOK_URL -R <OWNER>/short-ratio-intel
gh secret set JQUANTS_EMAIL -R <OWNER>/short-ratio-intel
gh secret set JQUANTS_PASSWORD -R <OWNER>/short-ratio-intel
```

`? Paste your secret:` のプロンプトが出たら右クリック貼り付け → Enter（マスク表示される）。

---

## ステップ5: ワークフローの動作確認（手動実行）

GitHub の **Actions タブ → 「空売り比率 定時取得・AIレポート生成」→ Run workflow** から手動実行。
モードは `full` / `fetch-only` / `no-news` を選べる。

> ⚠️ **`gh workflow run` をループやリトライで叩かないこと。** 一時的な500エラーでも、報告してから単発で1回だけ。
> （過去にリトライループでワークフロー60連続実行・メール100通の事故あり）

---

## ステップ6: Streamlit Community Cloud にデプロイ

1. https://share.streamlit.io → New app → 当該 Public リポジトリ / `main` / `app/streamlit_app.py` を指定
2. **Advanced settings → Secrets** に `.streamlit/secrets.toml.example` の内容を貼る（値は本物に置換）
3. ログイン用 bcrypt ハッシュを生成して `[auth]` に貼る:
   ```powershell
   python -c "import bcrypt; print(bcrypt.hashpw(b'好きなパスワード', bcrypt.gensalt()).decode())"
   ```
4. Deploy → 発行された `https://xxxx.streamlit.app` をスマホでブックマーク

### Secrets 変更後のトラブル時
- Manage app → **Reboot app** → ブラウザ **Ctrl+F5**（ハードリロード）→ 再ログイン
- `Invalid salt` が出たら `password_hash` に余計な文字（山括弧やスペース）が混じっていないか確認

---

## メモ

- Streamlit Cloud は secrets.toml の**トップレベル文字列キー**を `os.environ` にも反映するため、
  `config/settings.py` の `os.getenv()` がそのまま読む。`[auth]` セクションは `st.secrets` 経由（ログイン処理が直接参照）。
- スケジュールは `.github/workflows/daily_fetch.yml` の cron `0 10 * * 1-5`（=平日19:00 JST）。変更時はUTCで書く。
- ローカル開発は従来どおり `DATABASE_URL` 未設定で SQLite を使えば影響なし。
