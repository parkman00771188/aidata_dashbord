@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo AI-Hub 목록을 다시 받아 새 데이터셋만 추가 수집합니다...
python crawl_aihub.py --refresh
pause
