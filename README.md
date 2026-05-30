# short-ratio-intel

空売り比率インテリジェンス・システム
JPX空売り集計データ + 業種別空売り比率 + Gemini AI を統合した相場分析エンジン。

2026-05-30 にクラウド化済み。**PCに依存せず**、GitHub Actions が平日に自動取得し、スマホ/PCのブラウザから閲覧できる。

---

## 構成

```
GitHub Actions（平日19:00 JST）           Streamlit Community Cloud（常時）
  └ scripts/fetch_short_ratio.py            └ app/streamlit_app.py
       取得 → 市場テーマ判定 → AIレポート       Supabaseを読んでブラウザ表示
       │                                       │
       └──────────►  Supabase(PostgreSQL)  ◄───┘
```

- **データ取得**: JPX公式PDF + stock-marketdata.com（公開スクレイピング、認証キー不要）
- **AI**: Gemini API（既定 `gemini-3.5-flash`）
- **任意ニュース**: Tavily API（市場テーマ判定・レポート増補）
- **保存先**: 環境変数 `DATABASE_URL` があれば Supabase、無ければローカル SQLite に自動切替
- 閲覧はクラウドの公開URL（bcryptログインで保護）。データ取得は GitHub Actions が自動実行。

---

## ローカルで動かす（開発）

`DATABASE_URL` を設定しなければローカル SQLite で動く。

```powershell
# ワンクリック: アプリ起動.bat をダブルクリック（http://localhost:8501 が開く）
# またはコマンドで:
python -m streamlit run app\streamlit_app.py
```

初回は `.env.example` を `.env` にコピーし、`GEMINI_API_KEY` / `TAVILY_API_KEY` を設定する
（`DATABASE_URL` を入れるとローカルからも Supabase に接続する）。

---

## ドキュメント

- 日常運用・トラブルシューティング: [docs/operation_manual.md](docs/operation_manual.md)
- クラウド構築の再現手順（Supabase / GitHub Secrets / Streamlit Cloud）: [DEPLOY.md](DEPLOY.md)

---

## セキュリティ方針

- APIキー・接続文字列・ログイン情報は **GitHub Secrets / Streamlit Cloud Secrets** で管理し、リポジトリには含めない（`.env`・`.streamlit/secrets.toml`・`*.db` は `.gitignore` 済み）。
- 専有ナレッジ（思考データ）はリポジトリに置かず **Supabase に保存**する。更新は `python -m scripts.upload_knowledge_to_supabase`。
