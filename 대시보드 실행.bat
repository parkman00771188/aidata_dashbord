@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo AI-Hub 대시보드 서버를 시작합니다. (이 창을 닫으면 서버가 종료됩니다)
python serve.py
pause
