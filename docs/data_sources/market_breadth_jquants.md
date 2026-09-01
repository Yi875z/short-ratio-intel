# 騰落銘柄数・TOPIX の取得元（J-Quants API v2）

- 調査日: 2026-08-29 / 実装日: 2026-08-31
- 実装: `src/data_fetcher/jquants_api_client.py`（取得）/ `src/analyzer/market_breadth.py`（算出）
- 目的: 契約プランで何が取れて何が取れないかを確定させ、算出ルールを明文化する

## なぜ J-Quants を使うことにしたか

需給モニターに「値上がり・値下がり銘柄数」が必要になったが、**東証全体の騰落銘柄数を
無料・機械可読で出す確立した取得元が見つからなかった**（2026-08-29 調査）。

| 候補 | 結果 |
|---|---|
| stock-marketdata.com | 日本市場の騰落銘柄数ページが存在しない（`advance-decline-sp500.html` は米国のみ） |
| nikkei225jp.com | 日次JSONに該当系列なし |
| JPX 日次統計PDF（`est-set` / `boq`） | pypdf で本文抽出して確認。売買高・売買代金と債券相場表のみで騰落銘柄数なし |
| nikkei.com `/markets/kabu/japanidx/` | HTTP 200 で取れるが**当日値のみ・履歴なし**。表記変更に弱い |

J-Quants Light の契約により、**全銘柄日足から自前で数える**方式が可能になった。
公式データで、5年分のバックフィルもできる。日経のスクレイプ案は破棄した。

## 契約プランごとの可否（Light の実キーで実測）

ベースURL `https://api.jquants.com/v2`、認証は `x-api-key` ヘッダ（有効期限なし）。
v1 のメール+パスワードによる refreshToken → idToken の交換は不要。

| エンドポイント | 内容 | Light |
|---|---|---|
| `/equities/master` | 上場銘柄一覧（S33業種・市場区分 Mkt・規模区分） | 200 |
| `/equities/bars/daily` | 全銘柄日足（`date` 指定で1リクエスト・ページングなし） | 200 |
| `/indices/bars/daily/topix` | TOPIX 四本値 | 200 |
| `/markets/calendar` | 取引カレンダー（`HolDiv` "1"=営業日） | 200 |
| `/equities/investor-types` | 投資部門別（週次） | 200 |
| `/markets/short-ratio` | **業種別空売り比率** | **403** |
| `/indices/bars/daily` | TOPIX 以外の指数四本値（業種別指数など） | **403** |

**空売り比率は Light では取れない**（Standard 以上）。したがって空売り比率の取得元は
今後も JPX 公開PDF（`jpx_pdf_client.py`）が正である。
**業種別指数も取れない**ため、業種別騰落率は従来どおり nikkei225jp.com
（`src/macro_context/sector_price.py`）を使う。

市場区分コード: `0111` プライム / `0112` スタンダード / `0113` グロース /
`0109` その他（ETF・REIT・優先株等）/ `0105` TOKYO PRO MARKET。

## 騰落銘柄数の算出ルール

1. **母集団は対象日時点の銘柄一覧**（`/equities/master?date=対象日`）。
   省略すると翌営業日時点の一覧が返り、新規上場・市場変更のぶん母集団がずれる。
2. **騰落判定は調整後終値 `AdjC` 同士で比較する**。生値 `C` で比べると分割銘柄を誤判定する。
   実例（2026-08-28）: 68340 は生値 27,600 → 5,200 で暴落に見えるが、
   前日の `AdjC` は既に調整済み（5,520）なので実際は 5,520 → 5,200 の小幅安。
3. **前日または当日の足が無い銘柄は `not_compared` に積み、補間しない。**
4. **スコープ（市場区分）を必ず持たせる。**
   JPX 空売り集計は東証全体（外国株券等を含む。PDF注記で確認）で対象範囲が違うため、
   騰落銘柄数と空売り代金を跨いだ除算は行わない。

## 公表値との差（仕様として許容する）

2026-08-28 プライムの算出結果は 値上がり873 / 値下がり635 / 変わらず49（母集団1,557）。
日経公表値は 値上がり873 / 値下がり631。**値上がりは完全一致**し、値下がりに4銘柄
（0.26%）の差が出る。

分割調整の誤りではないことを確認済み。`AdjC` 比較と「前日C × 当日AdjFactor」補正は
4銘柄すべてで同一の判定になる。差は権利落ち銘柄などの集計慣行の違いによるもの。
**公表値への追随ではなく、公式データからの決定論的な再現性を優先する。**

## 保存先

`market_breadth_daily`（`src/storage/models.py`）。ユニークキーは `(date, market_scope)`。
TOPIX の終値・前日終値・騰落率は市場共通の文脈として同じ行に載せる。

## 運用

- 日次パイプライン（`scripts/fetch_short_ratio.py` の `_step_breadth`）が
  空売り比率の取得後に実行する。**fail-soft** で、失敗しても本処理を止めない。
- バックフィルは `python -m scripts.backfill_market_breadth --days 245`。
  連続営業日では前日の日足を使い回すため1営業日あたり2リクエスト。
  1年（245営業日）で約20分・980レコード。冪等なので再実行で続きから埋まる。
- レート制限は Light で 60 req/分。最短間隔 1.05 秒を空けている。

## 次に同じ症状が出たときの確認手順

```bash
# 1. キーが生きているか（TOPIXが返れば認証OK）
python -c "from dotenv import load_dotenv; load_dotenv(); \
from src.data_fetcher.jquants_api_client import JQuantsApiClient; \
print(JQuantsApiClient().get_topix_bars('2026-08-24','2026-08-28'))"

# 2. 契約プランの範囲を確認（403 なら plan を上げないと使えない）
python -c "from dotenv import load_dotenv; load_dotenv(); \
from src.data_fetcher.jquants_api_client import JQuantsApiClient; \
print(len(JQuantsApiClient().get_daily_bars('2026-08-28')))"   # 健全なら 4,400 前後

# 3. DBの欠測日を特定
python -c "from dotenv import load_dotenv; load_dotenv(); \
from src.storage.db import get_saved_market_breadth_dates; \
print(get_saved_market_breadth_dates()[:10])"

# 4. 欠測日を埋める（冪等）
python -m scripts.backfill_market_breadth --from YYYY-MM-DD --to YYYY-MM-DD
```

`This API is not available on your subscription` が返る場合は契約プランの問題であり、
リトライしても回復しない（クライアントも 403 では再試行しない）。

## 回帰テスト

- `tests/test_jquants_api_client.py` — 認証・ページング・403の即時打ち切り・再試行
- `tests/test_market_breadth.py` — 分割銘柄の扱い・母集団・スコープ分離・欠損
- `tests/test_market_breadth_db.py` — 冪等な保存・既存テーブルへの非干渉
- `tests/test_pipeline_breadth_failsoft.py` — 失敗してもパイプラインを止めないこと
