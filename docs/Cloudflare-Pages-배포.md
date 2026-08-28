# Cloudflare Pages 배포 (find-publicdata.pages.dev)

GitHub 저장소를 연결하면 푸시할 때마다 자동으로 다시 빌드·배포됩니다.
파이썬 서버 없이 **정적 JSON 샤드 + Pages Functions** 로 동작합니다.

| | 로컬 (`대시보드 실행.bat`) | Cloudflare Pages |
|---|---|---|
| 목록·검색·상세 | `serve.py` + SQLite(FTS5) | 정적 JSON 샤드 + 브라우저 검색 |
| 내 데이터 / 저장된 추천 | `data/*.json` 파일 | 브라우저 localStorage (방문자별) |
| AI 추천 | 내 PC의 Gemini 키 | Pages Functions + KV에 저장된 **사이트 키** |
| 설정 변경 | 누구나(내 PC니까) | **관리자 로그인 후에만** |

---

## 1. 처음 한 번만 하는 설정

### ① KV 네임스페이스 만들기
Cloudflare 대시보드 → **Storage & Databases → KV → Create namespace**
- 이름: `find-publicdata-settings` (아무 이름이나 가능)

> AI 설정(키·on/off·모델)과 키워드 캐시가 여기에 저장됩니다.

### ② Pages 프로젝트 만들기
**Workers & Pages → Create → Pages → Connect to Git** 에서 `aidata_dashbord` 저장소 선택 후:

| 항목 | 값 |
|---|---|
| 프로젝트 이름 | `find-publicdata` ← 주소가 `find-publicdata.pages.dev` 가 됩니다 |
| 프로덕션 브랜치 | `main` |
| 빌드 명령 | `sh build.sh` |
| 빌드 출력 디렉터리 | `site` |
| 루트 디렉터리 | (비워 둠) |

### ③ 바인딩·환경변수
프로젝트 → **Settings → Bindings** 에서 KV 추가:

| 종류 | 변수 이름 | 값 |
|---|---|---|
| KV namespace | `SETTINGS` | ①에서 만든 네임스페이스 |

**Settings → Variables and Secrets** (Production·Preview 모두 권장):

| 이름 | 필요성 | 설명 |
|---|---|---|
| `ADMIN_PASSWORD` | **강력 권장** | 관리자 비밀번호. 없으면 기본값 `admin123!@#` 이 쓰이는데, 이 값은 공개 저장소에 적혀 있어 누구나 알 수 있습니다. **반드시 바꾸세요.** |
| `ADMIN_USER` | 선택 | 관리자 아이디. 기본값 `admin` |
| `ADMIN_SECRET` | 선택 | 로그인 세션 서명 키. 임의의 긴 문자열 |
| `GEMINI_API_KEY` | 선택 | 관리자 화면에서 키를 넣는 대신 여기에 둘 수도 있습니다. 이때는 화면에서 "키 삭제"가 되지 않습니다. |

---

## 2. AI 추천 켜기 (관리자)

1. 사이트 오른쪽 위 **⚙ 설정** 클릭
2. 관리자 아이디 `admin` / 비밀번호 입력 후 **로그인**
3. **Gemini API 키** 입력 → **저장**
4. **AI 추천 기능 켜기** 체크

이제 방문자는 키 없이 AI 추천을 쓸 수 있습니다. **비용은 내 Gemini 계정에서 나갑니다.**

- 끄고 싶으면 체크 해제 → 방문자에게 "AI 추천이 꺼져 있습니다" 로 표시됩니다.
- **저장된 키 삭제** 로 키 자체를 지울 수도 있습니다.
- 키는 브라우저로 절대 내려보내지 않습니다(끝 4자리만 마스킹 표시).
- 로그인 세션은 12시간 유지되고, 쿠키는 HttpOnly·Secure·SameSite=Lax 입니다.

> ⚠️ 공개 주소이므로 **기본 비밀번호를 그대로 두면 누구나 관리자 화면에 들어와 키를 켜고 쓸 수 있습니다.**
> `ADMIN_PASSWORD` 환경변수를 꼭 설정하세요. 기본값을 쓰는 동안에는 설정 화면에 경고가 표시됩니다.

---

## 3. 빌드가 하는 일

`build.sh` → `build_static.py` 가 `data/snapshot/*.jsonl.gz`(깃에 포함)를 읽어 `site/` 를 만듭니다.

```
site/index.html            대시보드(로컬과 같은 화면)
site/static-api.js         /api/* 를 정적 샤드로 대체하는 데이터 계층
site/_headers              캐시·콘텐츠 타입
site/data/meta.json.gz     통계·수집시각·분야/형식 집계
site/data/idx-NN.json.gz   검색 색인 12개 (전체 84,933건)
site/data/page-NNNN.json.gz 첫 화면용 20페이지
site/data/det-NNNN.json.gz 상세 850개 (상세정보 + 미리보기 전체)
```

- 산출물 **약 147MB · 887개 파일** (Pages 제한: 파일당 25MiB, 총 20,000개 → 여유 있음)
- 샤드는 gzip 으로 저장하고 브라우저가 `DecompressionStream` 으로 풉니다.
  (`Content-Encoding` 헤더를 쓰면 CDN이 한 번 더 압축해 이중 압축이 되므로 일부러 쓰지 않습니다.)
- 빌드 시간 약 1분

### 데이터 갱신 흐름
```
로컬에서 수집 → python snapshot.py export → git push
                                              ↓
                                Cloudflare Pages 자동 재빌드·배포
```

---

## 4. 로컬에서 Pages 환경 그대로 확인하기

```bash
python build_static.py                              # site/ 생성
npx wrangler pages dev site --kv SETTINGS           # http://127.0.0.1:8788
```
Functions·헤더·KV까지 실제 Pages와 동일하게 동작합니다.

---

## 5. 공개 사이트에서 달라지는 점

- **내 데이터(★)와 저장된 추천은 방문자 브라우저에만** 저장됩니다. 기기·브라우저가 바뀌면 보이지 않습니다.
- 검색은 처음 한 번 **검색 색인(약 17MB, gzip)** 을 내려받은 뒤 브라우저에서 처리합니다.
  받는 동안 오른쪽 위에 "검색 색인 불러오는 중" 이 표시되고, 첫 목록은 미리 만들어 둔 페이지로 바로 보입니다.
- 상세·미리보기는 필요한 샤드만 그때그때 받습니다.
