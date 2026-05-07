# 空売り比率インテリジェンス 運用マニュアル

最終更新: 2026-05-08

---

## システム概要

JPXの空売り比率データ（J-Quants API）と空売り残高データ（JPX PDF）を取得・分析し、Gemini AIによるレポートを生成するStreamlitアプリ。

- **URL**: http://localhost:8501
- **データソース**: J-Quants API、JPX空売り残高PDF
- **AI**: Gemini API（AIレポートタブ）

---

## 起動方法

### ワンクリック起動（推奨）

`C:\CarSol\short-ratio-intel\アプリ起動.bat` をダブルクリックする。

- ポート競合が発生していても自動解消して起動する
- 起動後、ブラウザが自動的に http://localhost:8501 を開く
- **黒いウィンドウ（コマンドプロンプト）を閉じるとアプリも停止する**

### コマンドで起動する場合

```powershell
cd C:\CarSol\short-ratio-intel
python -m streamlit run app\streamlit_app.py
```

---

## 停止方法

`アプリ起動.bat` を実行して開いた黒いウィンドウを閉じる。またはそのウィンドウで `Ctrl+C` を押す。

---

## 日常運用の流れ

1. `アプリ起動.bat` をダブルクリックしてアプリを起動する
2. ブラウザが開いたら「データ取得」ボタンでデータを更新する
3. 各タブ（概要 / 業種 / JPX内訳 / 履歴 / AIレポート）で分析を確認する
4. AIレポートタブでGeminiによる相場考察を生成する
5. 確認が終わったら黒いウィンドウを閉じてアプリを停止する

---

## ファイル構成（重要箇所のみ）

```
C:\CarSol\short-ratio-intel\
├── アプリ起動.bat                      ← ワンクリック起動ファイル
├── app\
│   ├── streamlit_app.py               ← アプリ本体（現在はbytecodeローダー）
│   └── __pycache__\
│       └── streamlit_app_original.cpython-311.pyc  ← 【重要】絶対に削除しないこと
├── src\                               ← データ取得・分析・AIエンジン
├── config\                            ← 設定ファイル
├── data\                              ← SQLiteデータベース（Gitで除外）
└── .env                               ← APIキー（Gitで除外）
```

---

## ⚠️ 重要な注意事項

### `streamlit_app_original.cpython-311.pyc` について

現在の `app\streamlit_app.py` はスタブ（21行の起動ローダー）であり、実際のアプリロジックはすべて `app\__pycache__\streamlit_app_original.cpython-311.pyc`（コンパイル済みbytecode）に格納されている。

この `.pyc` ファイルを**削除するとアプリが起動しなくなる**。絶対に削除・移動しないこと。

背景: 2026-05-07、PowerShellの文字コード変換バグにより `streamlit_app.py` のソースコードが破損した。復旧は2026-05-08のbytecodeを利用した代替起動方式で完了している。

### `.env` ファイルについて

APIキーが記載された `.env` は Gitで管理されていない。PCを移行する際は手動でコピーすること。

---

## トラブルシューティング

### ポート8501がすでに使用中と表示される

`アプリ起動.bat` が自動的に解消する。手動で解消する場合は以下を実行する。

```powershell
# ポート8501を使っているPIDを確認
netstat -aon | findstr ":8501"
# 該当PIDを終了（例：PID=12345）
taskkill /PID 12345 /F
```

### アプリは起動するが「データなし」と表示される

データ取得ボタンを押してJ-QuantsからデータをダウンロードするかDBにデータが存在するか確認する。

### AIレポートが生成されない（TypeError: boolean value of NA is ambiguous）

このエラーは現在のbytecodeローダー方式で修正済み。発生した場合は `アプリ起動.bat` で再起動する。

---

## バックアップ運用

このプロジェクトはGit管理下にある（2026-05-08 初回コミット済み）。コードを変更した後は必ず以下を実行する。

```powershell
cd C:\CarSol\short-ratio-intel
git add -A
git commit -m "変更内容の説明"
```

GitHubへのリモートバックアップは任意のタイミングで設定する。設定済みであれば `git push` も実行する。
