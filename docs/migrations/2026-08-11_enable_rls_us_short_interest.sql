-- ------------------------------------------------------------------
-- 空売り残高テーブルの RLS 有効化（US-P3）
--
-- 実行タイミング: main へ push した後、GitHub Actions か Streamlit Cloud の起動で
--   us_short_interest テーブルが Supabase に自動生成された直後。
--   ローカルから日次パイプラインを実行した場合は、その時点で既に作られている。
--
-- 背景と注意点は 2026-08-06_enable_rls_us_tables.sql と同じ。
-- ⚠️ FORCE ROW LEVEL SECURITY は付けないこと（所有者接続も止まりアプリが壊れる）。
-- ⚠️ ポリシーの追加は不要（アプリは直接Postgres接続＝所有者ロールでRLSをバイパスする）。
-- ------------------------------------------------------------------

ALTER TABLE public.us_short_interest ENABLE ROW LEVEL SECURITY;

-- 検証: public スキーマ全テーブルの RLS 状態を実測する
SELECT
    c.relname                AS table_name,
    c.relrowsecurity         AS rls_enabled,
    c.relforcerowsecurity    AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
ORDER BY c.relrowsecurity, c.relname;
