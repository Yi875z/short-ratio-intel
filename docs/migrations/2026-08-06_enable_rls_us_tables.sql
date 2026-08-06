-- ------------------------------------------------------------------
-- 米国ショートフロー用テーブルの RLS 有効化（US-P1）
--
-- 実行タイミング: main へ push した後、最初の GitHub Actions 実行、または
--   Streamlit Cloud の起動によって Supabase に新テーブルが作られた直後。
--   SQLAlchemy の Base.metadata.create_all() がテーブルを自動生成するため、
--   放置すると RLS 無効のまま public スキーマに露出する。
--
-- なぜ必要か:
--   Supabase は public スキーマの全テーブルを PostgREST API として自動公開する。
--   anon キーは設計上「公開前提」の鍵なので、RLS 無効だと
--   プロジェクトURL + anon キーを知る者が全テーブルを読み書き・削除できる。
--
-- アプリが壊れない理由:
--   本アプリは DATABASE_URL による直接 Postgres 接続（postgres ロール＝テーブル所有者）で
--   繋いでいる。所有者は RLS をバイパスするため、ポリシー無しでも読み書きは通る。
--   遮断されるのは anon 経由の REST だけ＝狙いどおり。ポリシーの追加は不要。
--
-- ⚠️ FORCE ROW LEVEL SECURITY は絶対に付けないこと（所有者接続も止まりアプリが壊れる）。
-- ⚠️ 実行後に Info へ出る rls_enabled_no_policy は意図どおり。放置してよい。
-- ⚠️ 通知メール内の「Resolve issue」リンクは踏まず、
--    ブラウザで supabase.com/dashboard に直接ログインして SQL Editor から実行すること。
-- ------------------------------------------------------------------

ALTER TABLE public.us_short_volume_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.us_market_daily       ENABLE ROW LEVEL SECURITY;

-- ------------------------------------------------------------------
-- 検証: public スキーマ全テーブルの RLS 状態を実測する（推測しない）
-- rls_enabled が false の行が残っていないことを確認する。
-- rls_forced はすべて false であること。
-- ------------------------------------------------------------------

SELECT
    c.relname                AS table_name,
    c.relrowsecurity         AS rls_enabled,
    c.relforcerowsecurity    AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
ORDER BY c.relrowsecurity, c.relname;
