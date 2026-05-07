# ============================================================
# short-ratio-intel 初期セットアップスクリプト
# 実行: PowerShell で cd C:\CarSol\short-ratio-intel → .\setup.ps1
# ============================================================

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " short-ratio-intel セットアップ開始" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Python バージョン確認
Write-Host "`n[1/6] Python バージョン確認..." -ForegroundColor Yellow
python --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python が見つかりません。Python 3.11+ をインストールしてください。" -ForegroundColor Red
    exit 1
}

# 仮想環境作成
Write-Host "`n[2/6] 仮想環境 (.venv) を作成中..." -ForegroundColor Yellow
if (Test-Path ".venv") {
    Write-Host "  .venv は既に存在します。スキップします。" -ForegroundColor Gray
} else {
    python -m venv .venv
    Write-Host "  .venv 作成完了" -ForegroundColor Green
}

# 仮想環境を有効化
Write-Host "`n[3/6] 仮想環境を有効化中..." -ForegroundColor Yellow
.\.venv\Scripts\Activate.ps1

# pip アップグレード
Write-Host "`n[4/6] pip をアップグレード中..." -ForegroundColor Yellow
python -m pip install --upgrade pip --quiet

# 依存パッケージインストール
Write-Host "`n[5/6] 依存パッケージをインストール中..." -ForegroundColor Yellow
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: パッケージのインストールに失敗しました。" -ForegroundColor Red
    exit 1
}
Write-Host "  パッケージインストール完了" -ForegroundColor Green

# .env ファイル作成
Write-Host "`n[6/6] .env ファイルを確認中..." -ForegroundColor Yellow
if (Test-Path ".env") {
    Write-Host "  .env は既に存在します。スキップします。" -ForegroundColor Gray
} else {
    Copy-Item ".env.example" ".env"
    Write-Host "  .env を作成しました。APIキーを設定してください。" -ForegroundColor Green
}

# データディレクトリ確認
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
if (-not (Test-Path "data\reports")) { New-Item -ItemType Directory -Path "data\reports" | Out-Null }

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " セットアップ完了！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "次のステップ:" -ForegroundColor White
Write-Host "  1. .env ファイルにAPIキーを設定"
Write-Host "     - JQUANTS_EMAIL"
Write-Host "     - JQUANTS_PASSWORD"
Write-Host "     - GEMINI_API_KEY"
Write-Host ""
Write-Host "  2. ナレッジファイルを配置"
Write-Host "     src\knowledge\files\ に4つのMDファイルをコピー"
Write-Host ""
Write-Host "  3. アプリ起動"
Write-Host "     streamlit run app\streamlit_app.py"
Write-Host ""
