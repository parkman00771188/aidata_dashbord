@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo 공공데이터포털 파일데이터 전체 수집을 시작합니다.
echo 목록과 상세정보를 갱신한 뒤 남은 미리보기를 이어서 수집합니다.
echo 중간에 종료해도 이 파일을 다시 실행하면 이어집니다.
python crawl_data_go_kr.py --list --catalog --details --missing-previews --previews --refresh-list --workers 20
pause
