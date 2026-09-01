# short-ratio-intel

日本株の需給モニター。
JPX空売り集計（比率・代金）+ 市場流動性 + 価格反応 + 騰落銘柄数を突き合わせて
売り圧力レジームを判定し、Gemini AI がレポート化する。

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
- **騰落銘柄数・TOPIX**: J-Quants API v2（Light契約）。全銘柄日足から自前で数える。
  未設定でも動く（需給モニターに「未取得」と表示されるだけ。パイプラインは止まらない）
- **AI**: Gemini API（既定 `gemini-3.7-flash`。失敗時は `gemini-3.6-flash` → `gemini-3.5-flash` へ自動退避）
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

## 需給モニター

「⚖️ 需給モニター」タブで、空売り比率だけでなく**絶対額・市場流動性・価格反応**を
突き合わせて売り圧力を判定する。

- 上段は比率（分母＝合計売買代金）、代金（兆円）、価格と市場の広がりを別々に表示。
  比率が同じでも商いが半分なら実額は半分なので、両者を必ず分けて見る
- 中央はレジーム判定。`SELL_PRESSURE` / `THIN_MARKET` / `ABSORPTION` /
  `BROAD_DE-RISKING` / `SHORT_COVER_CANDIDATE` / `NEUTRAL` の6区分を、
  単一の固定閾値ではなく**直近20営業日分布に対するZスコアと5日平均比の組み合わせ**で決める。
  判定の根拠は実測値つきで表示し、入力が欠けたレジームは判定しない
- 下段は過去20営業日の推移。比率(%)と代金(兆円)は軸の意味が違うため段を分けている

騰落銘柄数のバックフィル: `python -m scripts.backfill_market_breadth --days 245`（1年ぶん）

---

## ドキュメント

- 日常運用・トラブルシューティング: [docs/operation_manual.md](docs/operation_manual.md)
- 騰落銘柄数・TOPIXの取得元と算出ルール: [docs/data_sources/market_breadth_jquants.md](docs/data_sources/market_breadth_jquants.md)
- 空売り比率の取得元: [docs/data_sources/short_ratio_karauri.md](docs/data_sources/short_ratio_karauri.md)
- クラウド構築の再現手順（Supabase / GitHub Secrets / Streamlit Cloud）: [DEPLOY.md](DEPLOY.md)

---

## セキュリティ方針

- APIキー・接続文字列・ログイン情報は **GitHub Secrets / Streamlit Cloud Secrets** で管理し、リポジトリには含めない（`.env`・`.streamlit/secrets.toml`・`*.db` は `.gitignore` 済み）。
- 専有ナレッジ（思考データ）はリポジトリに置かず **Supabase に保存**する。更新は `python -m scripts.upload_knowledge_to_supabase`。
