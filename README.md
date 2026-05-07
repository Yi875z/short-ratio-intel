# short-ratio-intel

空売り比率インテリジェンス・システム  
J-Quants API + JPX空売り残高PDF + Gemini AI 統合分析エンジン

---

## 起動方法

### ワンクリック起動（推奨）

`アプリ起動.bat` をダブルクリックする。ブラウザが自動的に http://localhost:8501 を開く。

### コマンドで起動する場合

```powershell
cd C:\CarSol\short-ratio-intel
python -m streamlit run app\streamlit_app.py
```

---

## 初回セットアップ

```powershell
cd C:\CarSol\short-ratio-intel
.\setup.ps1
```

セットアップ後、`.env` ファイルにAPIキーを設定する。

```
JQUANTS_EMAIL=your_email
JQUANTS_PASSWORD=your_password
GEMINI_API_KEY=your_key
```

---

## 詳細な運用手順

[docs/operation_manual.md](docs/operation_manual.md) を参照。

---

## ⚠️ 重要

`app/__pycache__/streamlit_app_original.cpython-311.pyc` は絶対に削除しないこと。  
アプリの実体がこのファイルに格納されている（詳細は運用マニュアル参照）。
