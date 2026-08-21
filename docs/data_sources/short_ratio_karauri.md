# 空売り比率の取得元と、2026-08 の表記変更による欠測

- 調査日: 2026-08-21
- 実装: `src/data_fetcher/jquants_client.py`（stock-marketdata）/ `src/data_fetcher/jpx_pdf_client.py`（JPX公式PDF）
- 目的: 取得元HTMLの「契約」を明文化し、次に表記が変わったときに当日中に気づけるようにする

## 取得元の役割

| 取得元 | 位置づけ | 取れるもの |
|---|---|---|
| JPX公式PDF（`*-m.pdf` / `*-g.pdf`） | **第一候補** | 実注文・価格規制あり/なしの金額内訳つき（34業種＋東証全体） |
| stock-marketdata.com `karauri.html` | フォールバック | 比率のみ（33業種＋東証全体、金額内訳なし） |

`jquants_client.py` は名前に反して J-Quants API を使わない。認証キーは不要。

## 2026-08 に起きたこと

2026-08-15〜08-17 の間に `karauri.html` の表記が3点同時に変わり、**完全一致でテーブルを探していた
パーサが全業種を取りこぼした**。8/18・8/20・8/21 の3営業日が欠測した。

| 箇所 | 変更前 | 変更後 |
|---|---|---|
| 業種名 | `水産農林業` `ガラス土石` `証券商品先物` `石油石炭製品` | `水産・農林業` `ガラス・土石製品` `証券、商品先物取引業` `石油・石炭製品` |
| 日付セル | `2026/08/14` | `2026年8月21日` |
| 東証全体テーブル 4列目 | `売買代金合計` | `売買代金` |

欠測が3日間気づかれなかった理由は表記変更そのものではなく、**壊れ方が静かだった**ことにある。

1. `fetch_and_store_recent_short_ratio()` が取得対象日リストを**スクレイパーの結果から**作っていたため、
   スクレイパーが0件を返した時点で候補日が空になり、**生きている JPX 公式PDF が一度も呼ばれなかった**。
2. 取得0件でも後段（テーマ判定・AIレポート）はDBの既存データで走り切るため、
   GitHub Actions は `success` で終了し、通知も出なかった。
   その結果、8/19 と 8/20 の実行はどちらも対象日 2026-08-19 の同じレポートを作っていた。

## 現在の実装が守っていること

- **業種名は正規化して引く**（`_normalize_sector_name()`）。中点・読点・空白を落とした形で
  `config/sectors.py` の canonical 名と突合するため、中点あり／なしの双方を吸収する。
  正規化しても一致しない省略形（`ガラス土石` `証券商品先物`）だけ `_LEGACY_SITE_ALIASES` で補う。
- **日付は複数表記を受ける**（`_parse_table_date()`）。`2026年8月21日` / `2026/08/21` / `2026-08-21`。
- **東証全体テーブルの4列目は前方一致**で見る（`売買代金` で始まればよい）。
- **取得対象日は両取得元の和集合**（`JPXShortSellingClient.get_available_dates()`）。
  片方が死んでももう片方で取得が続く。
- **取得0件はパイプラインを落とす**（`scripts/fetch_short_ratio.py` の `_step_fetch`）。
  非ゼロ終了 + Slack通知に乗るため、無通知の欠測にならない。

## 回帰テスト

- `tests/test_jquants_client.py` — 新旧どちらの表記のHTMLでも読めることをネットワーク非依存で固定
- `tests/test_pipeline_fetch_guard.py` — 取得0件で例外が飛ぶことを固定

## 次に同じ症状が出たときの確認手順

```bash
# 1. まず取得元が生きているか（HTTP 200 か、テーブルが見つかるか）
python -c "from src.data_fetcher.jquants_client import JQuantsClient; \
print(len(JQuantsClient().get_recent_days(5)))"      # 健全なら 165（33業種×5日）

# 2. JPX公式PDF側が生きているか
python -c "from src.data_fetcher.jpx_pdf_client import JPXShortSellingClient; \
print(JPXShortSellingClient().get_available_dates(5))"

# 3. DBの欠測日を特定
python -c "from dotenv import load_dotenv; load_dotenv(); \
from src.storage.db import get_saved_short_ratio_dates; print(sorted(get_saved_short_ratio_dates())[-10:])"

# 4. 欠測日を1日ずつ埋める（AIレポートまで作る場合は --no-theme --no-report を外す）
python -m scripts.fetch_short_ratio --date YYYY-MM-DD
```

`マッピング未定義の業種名: [...]` の WARNING がログに出ていたら、業種名の表記が
また変わったサインなので `_LEGACY_SITE_ALIASES` ではなく正規化規則の側を見直す。
