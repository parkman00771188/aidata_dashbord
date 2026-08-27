# AI-Hub 데이터셋 크롤링 & 로컬 대시보드

AI-Hub(https://aihub.or.kr) 의 "데이터 찾기" 목록(약 977건)과 각 상세 페이지를 수집해
JSON으로 관리하고, 로컬 대시보드로 훑어볼 수 있게 한 도구입니다.

## 파일 구성
| 경로 | 설명 |
|---|---|
| `crawl_aihub.py` | 크롤러. 목록 13페이지 + 상세 페이지 수집 후 파싱 |
| `data/datasets.json` | **목록 JSON(경량, ~2.4MB)**. `meta`(수집일·건수) + `datasets[]` — 대시보드 초기 로딩용 |
| `data/details/<sn>.json` | 데이터셋별 상세(메타데이터 구조표·데이터 통계·어노테이션·AI모델·구축업체 HTML 등). 상세 패널을 열 때만 로드 |
| `data/my_datasets.json` | **내 데이터셋**(담은 항목·데이터 있음 여부·메모·로컬 경로). 대시보드에서 변경하면 서버가 자동 저장 |
| `index.html` | 대시보드 (단일 파일) |
| `serve.py` | 로컬 서버(포트 8765). 정적 파일 제공 + `/api/my` 로 my_datasets.json 읽기/저장 |
| `대시보드 실행.bat` | `serve.py` 실행 + 브라우저 자동 오픈 |
| `데이터 갱신.bat` | 목록 다시 받고 **새 데이터셋만** 상세 추가 수집 → JSON 재생성 |
| `raw/list/`, `raw/view/` | 받아온 원본 HTML 캐시(dataSetSn 별). 있으면 재요청 안 함 |

## 사용법
```
pip install requests beautifulsoup4
python crawl_aihub.py            # 전체 수집(캐시 있으면 건너뜀) + JSON 생성   (~5분)
python crawl_aihub.py --refresh  # 목록 갱신 + 새 항목만 수집
python crawl_aihub.py --parse    # 재수집 없이 raw/ 에서 다시 파싱만
```
대시보드: `대시보드 실행.bat` 실행 (또는 `python serve.py`) → http://localhost:8765
`?sn=71982` 처럼 열면 해당 데이터셋 상세가 바로 뜹니다. (file:// 로 직접 열면 JSON을 읽지 못해 동작하지 않습니다)

## 대시보드 기능
**전체 데이터셋 탭**
- 제공 방식 태그(사이트 기준): `↓ 다운로드` (다운로드 신청 가능, `(승인)`은 개별 승인 필요) / `🔒 안심존 신청` (열람만, 다운로드 불가) / 준비중
- **내 보유**: `● 데이터 있음` 은 *내가 다운로드를 마쳤다고 표시한 것*만. 사이트의 다운로드 가능 여부와 별개
- ★ 담기 → 내 데이터셋에 추가. KPI 카드·분야/유형 칩·셀렉트 필터, 헤더 클릭 정렬, 페이지네이션(25/50/100/200)
- 행 클릭 → 상세 패널(개요·메타데이터 구조표·데이터 통계·어노테이션·활용 AI 모델·데이터 성능·구축 업체)

**내 데이터셋 탭**
- 담은 항목 수 / 전부 받으면 총 용량 / 데이터 있음 개수·보유 용량 / 남은 다운로드 개수·용량 (진행 바)
- 항목별 데이터 있음 토글, 메모, 로컬 경로 입력 → `data/my_datasets.json` 자동 저장
- JSON·CSV 내보내기, JSON 불러오기(병합)

## JSON 주요 필드 (`datasets[]`)
`sn`(dataSetSn), `title`, `url`, `field`(분야), `types`(유형[]), `gen_method`(생성방식), `build_year`, `update_ym`,
`size_bytes`, `views`, `downloads`, `likes`, `tags[]`, `status`, `approval_required`, `offline_available`, `has_sample`,
`s3_file_cnt`, `intro`, `purpose`, `meta{}`(메타데이터 구조표 key→value), `data_format`, `label_type`, `label_format`,
`build_amount`, `builder_main`, `builder_sub[]`
(상세 파일 `details/<sn>.json`: `sections{content,meta,stats,annotation,prcuseAi,prfomnc,autool,cnstcEntrps}`(정리된 HTML), `version_history`, `data_history`, `ai_perf`, `tabs`, `head_buttons`)

## 참고
- 사이트 표시 977건 중 975건이 수집됨: 페이지를 넘기는 사이 정렬(최신순)이 바뀌어 2건이 중복 노출된 것으로, 실제 누락은 아님. `--refresh` 로 재확인 가능.
- 파일 목록(파일명/개별 용량)은 로그인 후 AJAX로만 제공되어 수집 대상에서 제외.

## my_datasets.json 구조
```json
{"updated_at": "2026-08-26 16:00:00",
 "items": {"71982": {"sn": "71982", "title": "…", "field": "헬스케어", "size_bytes": 245182624311, "avail": "다운로드",
                      "url": "…", "have": true, "have_at": "2026-08-26", "memo": "", "path": "D:\data\nail", "added_at": "2026-08-26"}}}
```
