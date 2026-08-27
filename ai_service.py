# -*- coding: utf-8 -*-
"""Gemini 기반 데이터 추천 서비스.

로컬 카탈로그(catalog.db)에서 후보를 검색한 뒤 Gemini에게 추천/활용안을 요청한다.
API 키와 모델은 data/settings.json 에 저장한다(브라우저에는 키를 내려보내지 않는다).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from catalog_db import DATA_DIR, DB_PATH, connect, decode_json

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
RECO_PATH = os.path.join(DATA_DIR, "recommendations.json")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# 키를 저장하기 전에 보여줄 기본 목록. 키를 넣고 '모델 새로고침'을 누르면
# 계정에서 실제 사용 가능한 최신 모델 목록으로 완전히 대체된다.
FALLBACK_MODELS = [
    {"id": "gemini-flash-latest", "label": "Gemini Flash (항상 최신)", "family": "항상 최신 (별칭)",
     "note": "구글이 최신 Flash 모델로 자동 연결하는 별칭"},
    {"id": "gemini-pro-latest", "label": "Gemini Pro (항상 최신)", "family": "항상 최신 (별칭)",
     "note": "구글이 최신 Pro 모델로 자동 연결하는 별칭"},
    {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro Preview", "family": "Gemini 3",
     "note": "최신 세대 · 가장 정확, 느리고 비쌈"},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "family": "Gemini 2.5",
     "note": "정확도 우선"},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "family": "Gemini 2.5",
     "note": "속도·비용 균형 (기본값)"},
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "family": "Gemini 2.5",
     "note": "가장 빠르고 저렴"},
    {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash", "family": "Gemini 2.0",
     "note": "이전 세대"},
]
# 텍스트(JSON) 출력이 아닌 모델은 목록에서 제외한다.
# 이미지 생성(nano banana), 음악(lyria), 음성(tts/transcribe), 임베딩, 로보틱스, 실시간 등.
SKIP_MODEL_PARTS = ("embedding", "aqa", "imagen", "veo", "-tts", "image-generation",
                    "native-audio", "-live", "computer-use", "-image", "nano-banana",
                    "lyria", "transcribe", "robotics", "antigravity")
# 새로 설치했을 때 기본값 - 구글이 최신 Flash 로 자동 연결해 주는 별칭
DEFAULT_MODEL = "gemini-flash-latest"
FILE_LOCK = threading.Lock()

# 검색 정확도를 떨어뜨리는 흔한 낱말
STOPWORDS = {
    "데이터", "데이터셋", "자료", "정보", "관련", "필요", "필요한", "활용", "위한", "위해", "대한",
    "그리고", "또는", "있는", "있음", "하는", "해서", "때문", "부탁", "추천", "알려줘", "찾아줘",
    "관한", "각종", "전체", "모든", "여러", "다양한", "기반", "구축", "사용", "이용", "분석",
}


# --------------------------------------------------------------------------- 설정
def load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return {"api_key": "", "model": DEFAULT_MODEL, "updated_at": None}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"api_key": "", "model": DEFAULT_MODEL, "updated_at": None}
    data.setdefault("api_key", "")
    data.setdefault("model", DEFAULT_MODEL)
    return data


def save_settings(patch: dict) -> dict:
    with FILE_LOCK:
        data = load_settings()
        if "api_key" in patch:
            key = (patch.get("api_key") or "").strip()
            # 마스킹된 값이 그대로 돌아온 경우에는 기존 키를 유지한다.
            if key and "•" not in key:
                data["api_key"] = key
            elif patch.get("api_key") == "":
                data["api_key"] = ""
        if patch.get("model"):
            data["model"] = str(patch["model"]).strip()
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
    return data


def public_settings() -> dict:
    """브라우저로 내려보낼 설정(키는 마스킹)."""
    data = load_settings()
    key = data.get("api_key") or ""
    return {
        "has_key": bool(key),
        "key_hint": ("•" * 8 + key[-4:]) if len(key) >= 4 else ("•" * 8 if key else ""),
        "model": data.get("model") or DEFAULT_MODEL,
        "updated_at": data.get("updated_at"),
    }


class AiError(Exception):
    pass


# --------------------------------------------------------------------------- Gemini 호출
def _request(url: str, payload=None, key: str = "", timeout: int = 180):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-goog-api-key"] = key
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
            detail = json.loads(body).get("error", {}).get("message", "")
        except Exception:
            detail = body[:400]
        if e.code in (400, 401, 403) and ("API key" in detail or "API_KEY" in detail or e.code == 401):
            raise AiError("Gemini API 키가 올바르지 않습니다. 설정에서 키를 다시 확인해 주세요. (%s)" % detail[:200])
        if e.code == 404:
            raise AiError("선택한 모델을 사용할 수 없습니다. 설정에서 다른 모델을 선택해 주세요. (%s)" % detail[:200])
        if e.code == 429:
            raise AiError("Gemini 호출 한도(쿼터)를 초과했습니다. 잠시 후 다시 시도하거나 다른 모델을 선택해 주세요.")
        raise AiError("Gemini 오류 %s: %s" % (e.code, detail[:300]))
    except urllib.error.URLError as e:
        raise AiError("Gemini 서버에 연결하지 못했습니다: %s" % e.reason)
    except TimeoutError:
        raise AiError("Gemini 응답이 지연되어 시간 초과되었습니다. 더 빠른 모델(Flash)을 선택해 보세요.")


def _model_tier(lower: str) -> int:
    if "-pro" in lower:
        return 0
    if "flash-lite" in lower:
        return 2
    if "flash" in lower:
        return 1
    return 3


def _model_version(lower: str):
    m = re.match(r"gemini-(\d+)(?:[.\-](\d+))?", lower)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def model_family(model_id: str) -> str:
    lower = model_id.lower()
    if lower.endswith("-latest"):
        return "항상 최신 (별칭)"
    ver = _model_version(lower)
    if ver:
        return "Gemini %s" % (str(ver[0]) if ver[1] == 0 and "-%d." % ver[0] not in lower
                              else "%d.%d" % ver)
    if lower.startswith("gemma"):
        return "Gemma"
    return "기타"


def model_sort_key(model_id: str):
    """새 세대가 항상 위로 오도록 버전을 숫자로 비교한다(하드코딩 없음)."""
    lower = model_id.lower()
    tier = _model_tier(lower)
    stage = 1 if any(x in lower for x in ("preview", "-exp", "experimental")) else 0
    if lower.endswith("-latest"):
        return (0, 0, 0, tier, 0, lower)
    ver = _model_version(lower)
    if ver:
        return (1, -ver[0], -ver[1], tier, stage, lower)
    return (2, 0, 0, tier, stage, lower)


def list_models(key: str = "") -> list:
    key = key or load_settings().get("api_key") or ""
    if not key:
        return FALLBACK_MODELS
    models, token, pages = [], "", 0
    while pages < 6:
        url = GEMINI_BASE + "/models?pageSize=200" + (("&pageToken=" + urllib.parse.quote(token)) if token else "")
        data = _request(url, key=key, timeout=30)
        for m in data.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            mid = (m.get("name") or "").split("/")[-1]
            lower = mid.lower()
            if not mid or any(part in lower for part in SKIP_MODEL_PARTS):
                continue
            models.append({
                "id": mid,
                "label": m.get("displayName") or mid,
                "family": model_family(mid),
                "note": (m.get("description") or "").strip()[:110],
                "input_limit": m.get("inputTokenLimit") or 0,
                "output_limit": m.get("outputTokenLimit") or 0,
            })
        token = data.get("nextPageToken") or ""
        pages += 1
        if not token:
            break
    if not models:
        return FALLBACK_MODELS
    seen, unique = set(), []
    for m in sorted(models, key=lambda x: model_sort_key(x["id"])):
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        unique.append(m)
    return unique


def _extract_text(data: dict) -> str:
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise AiError("Gemini가 응답을 생성하지 않았습니다%s." % (" (차단 사유: %s)" % block if block else ""))
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        reason = cands[0].get("finishReason", "")
        if reason == "MAX_TOKENS":
            raise AiError("응답이 최대 길이를 초과했습니다. 더 짧게 질문하거나 다른 모델을 선택해 주세요.")
        raise AiError("Gemini 응답이 비어 있습니다%s." % (" (%s)" % reason if reason else ""))
    return text


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise AiError("Gemini 응답을 JSON으로 해석하지 못했습니다.")


def gemini_json(system: str, prompt: str, model: str = "", key: str = "", timeout: int = 180):
    settings = load_settings()
    key = key or settings.get("api_key") or ""
    model = model or settings.get("model") or DEFAULT_MODEL
    if not key:
        raise AiError("Gemini API 키가 설정되지 않았습니다. 오른쪽 위 설정에서 키를 입력해 주세요.")
    url = "%s/models/%s:generateContent" % (GEMINI_BASE, urllib.parse.quote(model))
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "responseMimeType": "application/json"},
    }
    data = _request(url, payload, key=key, timeout=timeout)
    return _parse_json(_extract_text(data))


# --------------------------------------------------------------------------- 로컬 검색
def tokenize(text: str) -> list:
    raw = re.split(r"[^0-9A-Za-z가-힣]+", text or "")
    out = []
    for token in raw:
        token = token.strip()
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in out:
            out.append(token)
    return out[:12]


def _rows_for(con, keywords, use_description: bool, limit_rows: int):
    if not keywords:
        return []
    cols = ["title", "file_name", "keywords_json"]
    if use_description:
        cols.append("description")
    clauses, args = [], []
    for kw in keywords:
        like = "%" + kw + "%"
        clauses.append("(" + " OR ".join("%s LIKE ?" % c for c in cols) + ")")
        args.extend([like] * len(cols))
    sql = (
        "SELECT uid,source,source_id,title,file_name,field,subfield,organization,formats_json,"
        "keywords_json,substr(description,1,600) description,modified_at,update_cycle,row_count,"
        "views,downloads,url,media_type,preview_status,"
        "json_extract(detail_json,'$.delivery') delivery,json_extract(detail_json,'$.extension') extension "
        "FROM catalog_items WHERE active=1 AND (" + " OR ".join(clauses) + ") "
        "ORDER BY downloads DESC, views DESC LIMIT ?"
    )
    args.append(limit_rows)
    return con.execute(sql, args).fetchall()


def search_candidates(keywords, fields=None, limit: int = 40) -> list:
    """키워드로 카탈로그를 검색하고 점수순 후보를 돌려준다."""
    keywords = [k for k in (keywords or []) if k][:10]
    if not keywords:
        return []
    con = connect(DB_PATH, readonly=True)
    try:
        rows = _rows_for(con, keywords, False, 900)
        if len(rows) < 40:
            rows = list(rows) + list(_rows_for(con, keywords, True, 900))
    finally:
        con.close()

    fields = set(fields or [])
    seen, scored = set(), []
    for row in rows:
        if row["uid"] in seen:
            continue
        seen.add(row["uid"])
        title = (row["title"] or "") + " " + (row["file_name"] or "")
        kws = row["keywords_json"] or ""
        desc = row["description"] or ""
        score = 0.0
        hits = 0
        for i, kw in enumerate(keywords):
            weight = 1.0 + max(0.0, (len(keywords) - i) / len(keywords))  # 앞쪽 키워드 가중
            hit = False
            if kw in title:
                score += 6 * weight
                hit = True
            if kw in kws:
                score += 3 * weight
                hit = True
            if kw in desc:
                score += 1.5 * weight
                hit = True
            hits += 1 if hit else 0
        if not hits:
            continue
        score += hits * 4  # 여러 키워드를 함께 만족하면 가산
        if row["field"] in fields:
            score += 5
        score += min(4.0, (row["downloads"] or 0) ** 0.35 / 6)
        if row["source"] == "AI Hub":
            score += 1.5  # AI 학습용 데이터는 목록이 적어 노출 기회를 보정
        scored.append((score, dict(row)))

    scored.sort(key=lambda x: -x[0])
    quota_ai = max(6, limit // 3)
    picked, ai_n, pub_n = [], 0, 0
    for score, row in scored:
        if len(picked) >= limit:
            break
        if row["source"] == "AI Hub":
            if ai_n >= quota_ai and len(picked) > limit * 0.6:
                continue
            ai_n += 1
        else:
            pub_n += 1
        row["_score"] = round(score, 2)
        picked.append(row)
    return picked


def access_of(row: dict, aihub_access: dict) -> dict:
    """제공 방식(다운로드 / 안심존 / 기관 자체 제공)을 통일된 형태로 만든다."""
    if row.get("source") == "AI Hub":
        info = aihub_access.get(str(row.get("source_id") or "")) or {}
        status = info.get("status") or ""
        if status == "안심존":
            return {"type": "안심존", "tone": "lock", "note": "이용신청 후 열람만 가능", "size_bytes": info.get("size_bytes", 0)}
        if status == "준비중":
            return {"type": "준비중", "tone": "wait", "note": "", "size_bytes": info.get("size_bytes", 0)}
        if status == "데이터 있음":
            return {"type": "다운로드", "tone": "download",
                    "note": "개별 승인 필요" if info.get("approval_required") else "",
                    "size_bytes": info.get("size_bytes", 0)}
        return {"type": "확인 필요", "tone": "muted", "note": "", "size_bytes": info.get("size_bytes", 0)}
    delivery = (row.get("delivery") or "").strip()
    ext = (row.get("extension") or "").upper()
    if "기관자체" in delivery or "URL" in delivery.upper():
        return {"type": "기관 제공", "tone": "wait", "note": "제공기관 사이트에서 다운로드", "size_bytes": 0}
    if "전자기록매체" in delivery:
        return {"type": "오프라인", "tone": "lock", "note": "전자기록매체 저장 제공", "size_bytes": 0}
    return {"type": "다운로드", "tone": "download", "note": ("%s 파일" % ext) if ext else "", "size_bytes": 0}


# --------------------------------------------------------------------------- 추천
KEYWORD_SYSTEM = (
    "너는 한국 공공데이터·AI 학습데이터 검색 전문가다. 사용자의 데이터 요구를 읽고 "
    "국내 데이터 카탈로그(공공데이터포털, AI Hub)에서 검색할 한국어 키워드를 뽑는다. "
    "카탈로그의 데이터명에 실제로 들어갈 법한 명사 위주로 만들고, '데이터'·'정보' 같은 일반어는 넣지 않는다. "
    "반드시 JSON만 출력한다."
)
KEYWORD_PROMPT = """사용자 요구:
\"\"\"{query}\"\"\"

아래 JSON 형식으로만 답하라.
{{
  "goal": "요구를 한 문장으로 정리",
  "keywords": ["검색 키워드 6~10개, 중요한 것부터"],
  "fields": ["다음 분야명 중 관련된 것만: {fields}"]
}}"""

RECO_SYSTEM = (
    "너는 데이터 기반 서비스 기획자다. 사용자의 목적과 '후보 데이터 목록'을 보고, "
    "목적 달성에 필요한 기능을 정의하고 후보 중에서 실제로 쓸 데이터를 골라 활용 방법을 제시한다. "
    "반드시 후보 목록에 있는 uid만 사용하고, 목록에 없는 데이터를 지어내지 않는다. "
    "후보 중 목적과 무관한 것은 제외한다. 모든 설명은 한국어로 쓰고 JSON만 출력한다."
)
RECO_PROMPT = """[사용자 목적]
\"\"\"{query}\"\"\"

[후보 데이터 목록]
{candidates}

위 후보만 사용해 아래 JSON 형식으로 답하라.
{{
  "title": "이 추천을 나타내는 12자 이내 제목",
  "goal": "목적을 1~2문장으로 정리",
  "features": [
    {{"name": "필요한 기능 이름", "detail": "무엇을 하는 기능인지 한두 문장", "data_need": "이 기능에 필요한 데이터 항목"}}
  ],
  "datasets": [
    {{"uid": "후보 목록의 uid", "role": "핵심|보조|참고",
      "why": "이 목적에 왜 필요한지 한두 문장",
      "usage": "구체적인 활용 방법",
      "items": ["활용할 주요 항목/컬럼 추정 3~6개"]}}
  ],
  "pipeline": [{{"step": "단계 이름", "detail": "그 단계에서 할 일"}}],
  "cautions": ["데이터 활용 시 유의사항 2~4개"],
  "missing": ["후보에 없어서 추가로 확보해야 할 데이터 1~3개"]
}}

규칙:
- datasets 는 중요도 순으로 최대 12개, '핵심'은 3~5개로 제한한다.
- features 는 3~6개, pipeline 은 3~6단계로 만든다.
- 후보에 마땅한 데이터가 없으면 datasets 를 비우고 missing 에 이유를 적는다."""


def _candidate_block(rows, aihub_access) -> str:
    lines = []
    for row in rows:
        acc = access_of(row, aihub_access)
        formats = ", ".join(decode_json(row.get("formats_json"), []) or [])
        kws = ", ".join((decode_json(row.get("keywords_json"), []) or [])[:6])
        desc = re.sub(r"\s+", " ", (row.get("description") or ""))[:220]
        lines.append(
            "- uid: {uid} | 출처: {source} | 데이터명: {title} | 분야: {field}{sub} | 제공기관: {org} | "
            "형식: {formats} | 제공방식: {acc} | 행수: {rows} | 갱신: {mod} | 키워드: {kws} | 설명: {desc}".format(
                uid=row["uid"], source=row["source"], title=row["title"], field=row.get("field") or "-",
                sub=(" > " + row["subfield"]) if row.get("subfield") else "", org=row.get("organization") or "-",
                formats=formats or "-", acc=acc["type"], rows=row.get("row_count") or "-",
                mod=row.get("modified_at") or "-", kws=kws or "-", desc=desc or "-")
        )
    return "\n".join(lines)


def recommend(query: str, model: str = "", aihub_access: dict | None = None, fields_available=None) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        raise AiError("찾고 싶은 데이터를 조금 더 자세히 적어 주세요.")
    aihub_access = aihub_access or {}
    started = time.time()
    settings = load_settings()
    model = model or settings.get("model") or DEFAULT_MODEL

    # 1단계 - 검색 키워드 추출
    fields_available = fields_available or []
    try:
        plan = gemini_json(
            KEYWORD_SYSTEM,
            KEYWORD_PROMPT.format(query=query, fields=", ".join(fields_available[:40])),
            model=model, timeout=90,
        )
    except AiError:
        raise
    keywords = [str(k).strip() for k in (plan.get("keywords") or []) if str(k).strip()][:10]
    keywords = [k for k in keywords if k not in STOPWORDS]
    if not keywords:
        keywords = tokenize(query)
    fields = [f for f in (plan.get("fields") or []) if f in set(fields_available)]

    # 2단계 - 로컬 카탈로그 검색
    candidates = search_candidates(keywords, fields, limit=42)
    if not candidates:
        candidates = search_candidates(tokenize(query), [], limit=42)
    if not candidates:
        raise AiError("카탈로그에서 관련 데이터를 찾지 못했습니다. 다른 표현으로 다시 검색해 보세요. (검색어: %s)" % ", ".join(keywords))

    # 3단계 - 추천 생성
    result = gemini_json(
        RECO_SYSTEM,
        RECO_PROMPT.format(query=query, candidates=_candidate_block(candidates, aihub_access)),
        model=model, timeout=240,
    )

    by_uid = {row["uid"]: row for row in candidates}
    datasets, dropped = [], 0
    for item in (result.get("datasets") or [])[:14]:
        row = by_uid.get(str(item.get("uid", "")).strip())
        if row is None:
            dropped += 1
            continue
        acc = access_of(row, aihub_access)
        datasets.append({
            "uid": row["uid"], "source": row["source"], "source_id": row["source_id"],
            "title": row["title"], "field": row.get("field") or "", "subfield": row.get("subfield") or "",
            "organization": row.get("organization") or "", "url": row.get("url") or "",
            "formats": decode_json(row.get("formats_json"), []) or [],
            "row_count": row.get("row_count") or 0, "downloads": row.get("downloads") or 0,
            "modified_at": row.get("modified_at") or "", "update_cycle": row.get("update_cycle") or "",
            "access": acc,
            "role": str(item.get("role") or "참고")[:6],
            "why": str(item.get("why") or ""), "usage": str(item.get("usage") or ""),
            "items": [str(x) for x in (item.get("items") or [])][:8],
        })

    payload = {
        "id": uuid.uuid4().hex[:12],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "query": query,
        "model": model,
        "title": str(result.get("title") or query)[:60],
        "goal": str(result.get("goal") or plan.get("goal") or ""),
        "keywords": keywords,
        "fields": fields,
        "features": [{"name": str(f.get("name") or ""), "detail": str(f.get("detail") or ""),
                      "data_need": str(f.get("data_need") or "")} for f in (result.get("features") or [])][:8],
        "datasets": datasets,
        "pipeline": [{"step": str(p.get("step") or ""), "detail": str(p.get("detail") or "")}
                     for p in (result.get("pipeline") or [])][:8],
        "cautions": [str(c) for c in (result.get("cautions") or [])][:6],
        "missing": [str(m) for m in (result.get("missing") or [])][:5],
        "candidate_count": len(candidates),
        "dropped": dropped,
        "elapsed": round(time.time() - started, 1),
    }
    save_reco(payload)
    return payload


# --------------------------------------------------------------------------- 추천 보관함
def load_recos() -> dict:
    if not os.path.exists(RECO_PATH):
        return {"updated_at": None, "items": []}
    try:
        with open(RECO_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("items", [])
        return data
    except Exception:
        return {"updated_at": None, "items": []}


def _write_recos(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(RECO_PATH), exist_ok=True)
    tmp = RECO_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RECO_PATH)


def save_reco(item: dict) -> dict:
    with FILE_LOCK:
        data = load_recos()
        data["items"] = [item] + [x for x in data["items"] if x.get("id") != item.get("id")]
        data["items"] = data["items"][:200]
        _write_recos(data)
    return data


def delete_reco(reco_id: str) -> dict:
    with FILE_LOCK:
        data = load_recos()
        data["items"] = [x for x in data["items"] if x.get("id") != reco_id]
        _write_recos(data)
    return data
