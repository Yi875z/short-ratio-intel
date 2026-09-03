-- 2026-09-03 JPX内訳の出所を列として記録する
--
-- なぜ必要か:
--   内訳4列は nullable=False, default=0 で定義されているため、DB上は
--   「未取得（スクレイパー由来で内訳を持たない）」と「本当に0」を区別できない。
--   全レイヤが「3列いずれかが非0なら内訳あり」というヒューリスティクスに依存しており、
--   その判定が db / pressure_metrics / flow_signal_analyzer / prompt_builder /
--   streamlit_app の5ファイルに散っていた。推測が散っている状態は次の事故の温床になる。
--
--   列を nullable にして NULL を入れる案もあるが、既存行の意味が変わるうえ
--   「0 と書いてあるが本当に0なのか」を後から判定できない。出所を積極的に
--   記録するほうが、既存行を触らずに事実を確定できる。
--
-- 値:
--   'jpx_pdf' … JPX公式PDF由来。内訳4列は実測値。
--   'scraper' … stock-marketdata 由来。内訳4列は 0 で、値が無いことを意味する。
--
-- 冪等。何度流してもよい。ロールバックは DROP COLUMN breakdown_source。

ALTER TABLE public.market_short_ratio_daily
  ADD COLUMN IF NOT EXISTS breakdown_source VARCHAR(16);

ALTER TABLE public.short_ratio_daily
  ADD COLUMN IF NOT EXISTS breakdown_source VARCHAR(16);

-- 既存行の分類は、これまで全レイヤが使ってきたヒューリスティクスと同じ条件で
-- 一度だけ確定させる。以後コード側は推測をやめ、この列を読む。
UPDATE public.market_short_ratio_daily
SET breakdown_source = CASE
        WHEN COALESCE(total_short_va, 0) <> 0
          OR COALESCE(shrt_with_res_va, 0) <> 0
          OR COALESCE(shrt_no_res_va, 0) <> 0
        THEN 'jpx_pdf'
        ELSE 'scraper'
    END
WHERE breakdown_source IS NULL;

UPDATE public.short_ratio_daily
SET breakdown_source = CASE
        WHEN COALESCE(total_short_va, 0) <> 0
          OR COALESCE(shrt_with_res_va, 0) <> 0
          OR COALESCE(shrt_no_res_va, 0) <> 0
        THEN 'jpx_pdf'
        ELSE 'scraper'
    END
WHERE breakdown_source IS NULL;

-- 確認用:
--   SELECT breakdown_source, COUNT(*) FROM public.market_short_ratio_daily GROUP BY 1;
--   SELECT breakdown_source, COUNT(*) FROM public.short_ratio_daily GROUP BY 1;
