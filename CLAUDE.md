## プロジェクト概要（short-ratio-intel）

JPXの空売り比率を取得・分析し Gemini AIレポートを生成する Streamlit アプリ。**2026-05-30 クラウド化完了**。

- **構成**: GitHub Actions（平日19:00 JST = cron `0 10 * * 1-5`、`scripts/fetch_short_ratio.py`）→ Supabase(PostgreSQL) → Streamlit Community Cloud（`app/streamlit_app.py`、bcryptログイン）。PC非依存で稼働。
- **DB切替**: `src/storage/db.py` の `get_engine()` が `DATABASE_URL` ありで Supabase、無しでローカル SQLite。
- **データ取得**: J-Quants APIキーは**使わない**。`jquants_client.py` は名前に反し stock-marketdata.com のスクレイパー、`jpx_pdf_client.py` はJPX公開PDF取得。認証鍵不要。
- **外部ナレッジ**: 公開リポにIPを置けないため `knowledge_documents`（Supabase）に保存。原本は `C:\CarSol\knowledgefile\*.md`。更新は `python -m scripts.upload_knowledge_to_supabase`。読込は `loader.py` が Supabase優先→ローカルfallback。
- 詳細手順は `docs/operation_manual.md`、クラウド再構築は `DEPLOY.md`。

### 厳守ルール
- **リポジトリは Public**。`.env`・`.streamlit/secrets.toml`・`*.db` は `.gitignore` 済み。APIキー・接続文字列・パスワードを追跡ファイルやドキュメントに**絶対に書かない**。秘密は GitHub Secrets / Streamlit Cloud Secrets で管理。
- **`gh workflow run` をループ/自動リトライで叩かない**（単発・手動・1回のみ。過去に連続実行事故あり）。
- **Streamlit Cloud の Python は 3.12 固定**（既定の新しいPythonだと固定依存の wheel が無くビルド失敗する）。
- AIレポートのJSONは `gemini_client.py` で `max_output_tokens=32768`＋`json-repair` で堅牢化済み。
- コード変更後 main へ push すると Streamlit Cloud が自動再デプロイする。

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
