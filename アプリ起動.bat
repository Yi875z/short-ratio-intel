@echo off
chcp 65001 > nul
title 空売り比率インテリジェンス

echo ==========================================
echo  空売り比率インテリジェンス 起動中...
echo ==========================================
echo.

cd /d "C:\CarSol\short-ratio-intel"

:: ポート8501が使用中なら既存プロセスを終了
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8501 "') do (
    echo [情報] ポート8501を使用中のプロセス (PID:%%a) を終了します...
    taskkill /PID %%a /F > nul 2>&1
)

echo [起動] アプリを起動しています...
echo [情報] ブラウザが自動的に開きます (http://localhost:8501)
echo [停止] このウィンドウを閉じるとアプリが停止します
echo.

python -m streamlit run app\streamlit_app.py

pause
