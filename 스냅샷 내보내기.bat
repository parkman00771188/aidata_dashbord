@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo 깃에 올릴 수 있도록 카탈로그 스냅샷을 만듭니다. (data/snapshot)
python snapshot.py export
echo.
python snapshot.py info
pause
