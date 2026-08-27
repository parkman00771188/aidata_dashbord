@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo AI-Hub 목록을 다시 받아 새 데이터셋만 추가 수집합니다...
python crawl_aihub.py --refresh
echo.
echo 공공데이터포털 목록과 상세 메타데이터를 갱신합니다...
python crawl_data_go_kr.py --list --catalog --details --refresh-list
echo.
echo 행 미리보기 전체 수집은 "공공데이터 전체 수집.bat"를 실행하세요.
pause
