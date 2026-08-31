# Cloudflare Pages 배포 (publicdata-finder.pages.dev)

GitHub 저장소를 연결하면 푸시할 때마다 자동으로 다시 빌드·배포됩니다.
파이썬 서버 없이 **정적 JSON 샤드 + Pages Functions** 로 동작합니다.

| | 로컬 (`실행.bat` → `[4]`) | Cloudflare Pages |
|---|---|---|
| 목록·검색·상세 | `serve.py` + SQLite(FTS5) | 정적 JSON 샤드 + 브라우저 검색 |
| 내 데이터 / 저장된 추천 | `data/*.json` 파일 | 브라우저 localStorage (방문자별) |
| AI 추천 | 내 PC의 Gemini 키 | Pages Functions + KV에 저장된 **사이트 키** |
| 설정 변경 | 누구나(내 PC니까) | **관리자 로그인 후에만** |

---

## 1. 배포하기 — `실행.bat` (권장)

**`실행.bat` 을 더블클릭하고 `[1] 배포하기` 를 고르면 끝입니다.** 약 5분 걸립니다.

```text
스냅샷 만들기 → 정적 사이트 빌드 → GitHub 커밋·푸시 → Cloudflare Pages 배포
```

처음 한 번만 Cloudflare 로그인이 필요합니다.

```bash
npx wrangler login          # 브라우저에서 승인 (한 번만)
```

로그인이 안 돼 있으면 배포 단계에서 그 사실을 알려 주고 멈추므로, 안내대로 로그인한 뒤
다시 `[1]` 을 고르면 됩니다.

### 손으로 하고 싶을 때

```bash
python snapshot.py export                                      # data/snapshot/*.jsonl.gz
python build_static.py                                         # site/ 생성 (약 1분)
npx wrangler pages deploy site --project-name publicdata-finder --branch main
```

프로젝트를 처음 만드는 경우에만 한 번 더:

```bash
npx wrangler pages project create publicdata-finder --production-branch main
```

### KV 연결 (AI 추천을 쓰려면 필요)
대시보드 → **Workers & Pages → publicdata-finder → Settings → Bindings → Add → KV namespace**

| 변수 이름 | 값 |
|---|---|
| `SETTINGS` | 미리 만들어 둔 KV 네임스페이스 (예: `publicdata-finder-settings`) |

바인딩을 추가한 뒤 한 번 더 배포하면 적용됩니다.

---

## 1-B. 방법 B: GitHub 연동 (푸시하면 자동 배포)

> ⚠️ 요즘 Cloudflare 는 Git 연동 시 **Worker** 로 만드는 경우가 많습니다.
> 그러면 `wrangler deploy` 가 실행되면서 `Missing entry-point to Worker script` 오류가 납니다.
> 반드시 **Pages** 로 만들어야 합니다.

**Workers & Pages → Create → `Pages` 탭 → Connect to Git** 에서 저장소 선택 후:

| 항목 | 값 |
|---|---|
| 프로젝트 이름 | `publicdata-finder` |
| 프로덕션 브랜치 | `main` |
| **빌드 명령** | `sh build.sh` ← **비워 두면 배포가 실패합니다** |
| **빌드 출력 디렉터리** | `site` |
| 루트 디렉터리 | (비워 둠) |

그다음 위와 같이 **Settings → Bindings** 에서 KV 를 `SETTINGS` 로 연결합니다.

### 환경변수 (Settings → Variables and Secrets)

| 이름 | 필요성 | 설명 |
|---|---|---|
| `ADMIN_PASSWORD` | **강력 권장** | 관리자 비밀번호. 없으면 기본값 `admin123!@#` 이 쓰이는데 이 값은 공개 저장소에 적혀 있습니다. **반드시 바꾸세요.** |
| `ADMIN_USER` | 선택 | 관리자 아이디. 기본값 `admin` |
| `ADMIN_SECRET` | 선택 | 로그인 세션 서명 키. 임의의 긴 문자열 |
| `GEMINI_API_KEY` | 선택 | 키를 환경변수로 둘 수도 있습니다(이때는 화면에서 "키 삭제"가 안 됩니다) |

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
실행.bat → [1] 배포하기
   스냅샷 → 빌드 → GitHub 푸시 → Pages 배포   (한 번에 처리)
```
수집부터 다시 하려면 `[2] 전체 실행` 을 고릅니다.

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
