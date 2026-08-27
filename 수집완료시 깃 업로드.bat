@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo 공공데이터 미리보기 수집이 끝나기를 기다렸다가
echo 스냅샷을 만들어 GitHub에 자동으로 올립니다.
echo 이 창을 닫으면 중단됩니다. (다시 실행하면 이어서 대기합니다)
echo.
python publish_when_done.py
pause
