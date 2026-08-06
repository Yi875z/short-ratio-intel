# 米国ショートフロー統合計画 2026-08-06

**v2仕様書（short-ratio-intel_v2_SPEC.md）の検証結果と、既存システムに適合させた実装計画**

- 作成日: 2026-08-06
- 入力: `C:\Users\yioku\Downloads\short-ratio-intel_v2_SPEC.md`（Web版Claude作成）
- 位置づけ: 改善計画書 2026-07-06 と並走する「USトラック」。既存フェーズ2（2-1業種別騰落率突合）とは独立。

---

## 1. 結論

v2仕様書は**ドメイン設計（何を計算し、何を禁止するか）は優秀だが、実装計画（どう作るか）の前提が現実のリポジトリと不一致**。
よって「Section 5 の分析ロジックと Section 8 のQCルールを採用し、Phase P0（全面リファクタ）は不採用。
USを既存アーキテクチャの並列モジュールとして追加する」方針に再設計する。

## 2. 前提検証の結果（2026-08-06 実施）

仕様書が「最重要」とする Phase P0 の動機＝`SellExShortVal` KeyError は**現行コードに存在しない**。

- 実フィールド名は `SellExShortVa`（末尾に l 無し）。`src/storage/db.py` の upsert は
  `r.get("SellExShortVa", 0)` とデフォルト付き `.get()` で全補助フィールドを防御済み。KeyError は構造的に起きない。
- 仕様書の前提「v1.x = 日本株スクレイパー + SQLite」も旧情報。実態は
  Supabase(PostgreSQL)本番 + SQLite開発fallback / SQLAlchemy / GitHub Actions 平日19:07 JST /
  Streamlit Cloud（bcrypt認証）/ Gemini 3.6 Flash AIレポート / pytest 72件 の本番稼働システム。
- 仕様書のディレクトリ再編（`src/short_ratio_intel/` パッケージ化・新CLI・生SQL DDL・既存テーブルの `_v1` 退避）は
  PROJECT_RULES の「最小差分」「既存動作を壊さない」に反し、72テスト・Streamlit・Actionsを同時に壊すリスクだけが残る。

一方、以下は当日実データで**こちらでも独立に疎通検証済み**:

- FINRA CNMS: `https://cdn.finra.org/equity/regsho/daily/CNMSshvol20260805.txt` → HTTP 200、539KB、
  ヘッダ `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market` は仕様書どおり。
- **仕様書の誤り1件**: `ShortVolume` / `TotalVolume` は int ではなく**小数を含む**
  （例: `211769.129173`。fractional share 報告由来と推定・意味は未確認）。パーサは Float 受けにする。
- 価格データ: Yahoo Finance chart API（`query1.finance.yahoo.com/v8/finance/chart/NVDA?range=...&interval=1d`）
  → HTTP 200。yfinance ライブラリは使わない（Streamlit Cloudでの失敗歴・依存追加回避。取得は Actions 側で実行）。

## 3. 設計方針（仕様書からの変更点）

1. **既存JPパイプラインは無変更**。USは `src/data_fetcher/finra_client.py` 以下の並列新モジュール。
2. **ストレージ層で日米を統一しない**（仕様書 Canonical Schema `ShortVolRecord` の日米共用は不採用）。
   日本側は「業種別 × 売買代金(JPY)ベース」、米国側は「銘柄別 × 株数ベース」で、粒度も単位も異なる。
   無理に1テーブルへ入れると単位混同事故の温床になる。**統合はレポート層・Streamlit UI層で行う**。
   （US内部の「欠落フィールドは明示的にNone」という契約思想自体は採用する）
3. 新テーブルは SQLAlchemy モデルで追加: `us_short_volume_daily`（PK: date+ticker+source）/
   `us_market_daily`（OHLCV・consolidated volume。**比率の分母使用禁止**）/ `us_short_interest`（P3・隔週残高）。
   既存テーブルのマイグレーションは行わない。**Supabase側で新テーブルのRLS有効化を必ず実施**（全社ルール）。
4. レポートは P1〜P2 ではルールベース Markdown + Slack通知（Gemini APIを消費しない）。
   AI統合（夕方レポートへのUS文脈ブロック注入）は P3。
5. 新規シークレット不要（FINRA・Yahoo ともキーレス）。公開リポのままで安全。

## 4. フェーズ計画

### US-P1 — 取得基盤とZ-Score（MVP・最初のマイルストーン）

- `src/data_fetcher/finra_client.py`: CNMS日次取得。キャッシュ（`data/cache/finra/`・gitignore）、
  ネットワークエラーのみ指数バックオフ3回、404/Access Denied は「非営業日」として静かに空を返す。
  Float対応・`BRK/B` 形式対応。バックフィル関数（ローリング12ヶ月、リクエスト間隔0.5〜1.0秒）。
- `config/us_universe.py`: 監視銘柄定数（`config/sectors.py` と同じ流儀。YAMLは導入しない）。
  初期ユニバース: 仕様書の24銘柄（semi_core/equip/adjacent + SMH/SOXX/QQQ/SPY）。レバレッジETFは対象外。
- `src/storage/models.py` に `UsShortVolumeDaily` / `UsMarketDaily` 追加、`db.py` に upsert
  （既存流儀の dict + `.get(None)` 防御。冪等: 同一 date+ticker の再投入で行数不変）。
- `src/data_fetcher/us_price_client.py`: Yahoo chart API で日足OHLCV取得（CLV・出来高比の材料）。
- `src/analyzer/us_flow_analyzer.py`（第1弾）: `finra_short_ratio = ShortVolume / TotalVolume`（**同一ソース内で完結**）、
  z20 / z60 / pct60。サンプル数が窓幅80%未満またはSD=0なら None（判定しない）。
- `scripts/backfill_us_short_flow.py` を1回実行し、250営業日分を Supabase へ投入。
- テスト追加: パーサ（小数・`BRK/B`）/ 非営業日応答 / upsert冪等 / zscore境界。
- **完了条件**: バックフィル後、任意銘柄の z20 が計算でき、テスト全件パス。

### US-P2 — 分類・バスケット・配信

- 4象限分類（`SELL_PRESSURE` / `SHORT_ABSORBED` / `LONG_LIQUIDATION` / `SQUEEZE_BUILDING`。
  全タグ candidate 扱い・断定表現禁止＝既存 report_lint の思想を踏襲）。
- バスケット集計: `Σ short_volume / Σ total_volume` の**ボリューム加重**（単純平均禁止）。
  SEMI20 / SEMI_CORE7 と、SMH・SOXX の ETF乖離（divergence = z20(ETF) − z20(構成銘柄加重)）、QQQ/SPY並置。
- `scripts/fetch_us_short_flow.py`: 日次実行本体。Markdownレポート生成 + 既存 `_notify_slack` 流用のハイライト通知。
- `.github/workflows/us_daily_fetch.yml`: **平日 JST 08:30**（cron `30 23 * * 0-4` UTC）。
  FINRA公開は米東部18:00頃＝JST 翌朝07:00（EDT）/ 08:00（EST）。冬時間の遅延余地をとって08:30。
  当日ファイル未公開なら fail-soft でスキップ（`gh workflow run` のループ再試行は禁止・既存ルールどおり）。
- Streamlit に「米国ショートフロー」タブ追加（バスケットz、乖離、アラート表、銘柄別時系列）。
- **完了条件**: 朝の自動実行でレポート+Slack通知が届き、Streamlitで閲覧できる。

### US-P3 — 残高と日米統合

- Short Interest（隔週残高）取り込みと、フロー系タグの `CONFIRMED` 昇格ロジック。取得経路は要調査（未確認）。
- 夕方の既存AIレポートへ「米国半導体ショートフロー」文脈ブロックを注入
  （`prompt_builder.py` 拡張・12,000字クリップ規律に従う）。日本の業種別空売りと同じ画面で日米が読める状態が最終形。
- Flow Score 出力（Decision Engine 連携は NEO Vault 側の仕様確定後。未確認）。
- Off-Exchange 比率（`TotalVolume / consolidated volume`）の時系列（これは分母混同ではなく意味のある補助指標）。

## 5. 採用するQCルール（仕様書 Section 8 + 追加2件）

仕様書の禁止7項目（分母混同禁止 / フロー・残高の混同禁止 / 絶対閾値禁止 / 単日断定禁止 /
単純平均バスケット禁止 / 欠損補間禁止 / upsertの個別フィールド引数禁止）は全採用。追加:

8. **日米の単位を跨いだ比較演算をしない**（JP=売買代金JPY・業種別、US=株数・銘柄別。並置表示はOK、加減乗除はNG）。
9. **FINRA数値はFloatで受ける**（小数を含む実データを確認済み。intキャストでの丸め・例外を作らない）。

## 6. 既存改善計画との優先順位

USトラックはJP側と依存関係がなく、並走可能。ただし着手順の推奨は
**US-P1（小さい・バックフィルで即250日の歴史が手に入る）→ JP フェーズ2-1（業種別騰落率突合・最重要のまま）→ US-P2**。
US-P1 を先に済ませておけば、P2/P3 をいつ実装しても Z-Score は初日から有効。

## 7. リスク・未確認事項

- FINRA `ShortVolume` の小数値の正確な意味（fractional share と推定）— 未確認。Float受けで実害はない。
- Yahoo chart API の GitHub Actions ランナーからの安定性 — ローカル疎通のみ確認。P1で実測。
- Short Interest の無料取得経路（FINRA公表ファイル or Nasdaq）— P3着手時に調査。
- Decision Engine（omni-market-agent v5）の受け取り仕様 — NEO Vault 側と要すり合わせ。
- テスト基準件数は増加後に PROJECT_RULES.md を更新する（現行72件）。
