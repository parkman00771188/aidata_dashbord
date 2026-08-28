# -*- coding: utf-8 -*-
"""Gemini 기반 데이터 추천 서비스.

로컬 카탈로그(catalog.db)에서 후보를 검색한 뒤 Gemini에게 추천/활용안을 요청한다.
API 키와 모델은 data/settings.json 에 저장한다(브라우저에는 키를 내려보내지 않는다).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from catalog_db import DATA_DIR, DB_PATH, connect, decode_json, has_fts

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
RECO_PATH = os.path.join(DATA_DIR, "recommendations.json")
KEYWORD_CACHE_PATH = os.path.join(DATA_DIR, "keyword_cache.json")
SAFEZONE_MANUAL_URL = ("https://aihub.or.kr/web-nas/aihub21/files/public/"
                       "%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4_%EC%95%88%EC%8B%AC%EC%A1%B4"
                       "%EC%82%AC%EC%9A%A9%EC%9E%90_%EB%A9%94%EB%89%B4%EC%96%BC.pdf")
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

# 100만 토큰당 단가(USD, 입력/출력). 공개 단가가 바뀌면 이 표만 고치면 된다.
# 표에 없는 모델은 같은 등급(pro/flash/flash-lite)의 단가로 추정한다.
PRICING = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
}
TIER_PRICING = {0: (1.25, 10.00), 1: (0.30, 2.50), 2: (0.10, 0.40), 3: (0.30, 2.50)}
USD_KRW = 1400.0  # 환율은 대략치 - 비용은 참고용 추정치다
FILE_LOCK = threading.Lock()

# 검색 정확도를 떨어뜨리는 흔한 낱말
STOPWORDS = {
    "데이터", "데이터셋", "자료", "정보", "관련", "필요", "필요한", "활용", "위한", "위해", "대한",
    "그리고", "또는", "있는", "있음", "하는", "해서", "때문", "부탁", "추천", "알려줘", "찾아줘",
    "관한", "각종", "전체", "모든", "여러", "다양한", "기반", "구축", "사용", "이용", "분석",
    # 질문 문장에서 흔히 나오는 서술어·군더더기
    "합니다", "입니다", "습니다", "하려고", "정하려고", "만들려고", "만들고", "싶습니다", "싶어요",
    "있습니다", "찾고", "알고", "하고", "통해", "결합", "엮어", "활용해", "이런", "저런", "어떤",
    "무엇", "생각", "고민", "계획", "방법", "경우", "중인", "현재", "지금", "정도", "가지",
}
# 조사를 떼어 내기 위한 접미사 목록(긴 것부터 검사)
PARTICLES = ("으로부터", "에서부터", "에게서", "으로서", "으로써", "이라는", "에서", "에게", "부터",
             "까지", "보다", "처럼", "이나", "라도", "으로", "이랑", "와", "과", "을", "를", "이",
             "가", "은", "는", "의", "에", "도", "만", "로", "랑")


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


def _collect_usage(data: dict, usage) -> None:
    """응답의 토큰 사용량을 누적한다(thoughts 는 출력 토큰으로 과금된다)."""
    if usage is None:
        return
    u = data.get("usageMetadata") or {}
    usage["prompt"] = usage.get("prompt", 0) + int(u.get("promptTokenCount") or 0)
    usage["output"] = usage.get("output", 0) + int(u.get("candidatesTokenCount") or 0)
    usage["thoughts"] = usage.get("thoughts", 0) + int(u.get("thoughtsTokenCount") or 0)
    usage["total"] = usage.get("total", 0) + int(u.get("totalTokenCount") or 0)
    usage["calls"] = usage.get("calls", 0) + 1


def estimate_cost(model: str, usage: dict) -> dict:
    """토큰 사용량으로 대략적인 비용을 계산한다(공개 단가표 기준 추정)."""
    model = model or DEFAULT_MODEL
    rate, exact = PRICING.get(model), True
    if rate is None:
        rate, exact = TIER_PRICING[_model_tier(model.lower())], False
    inp = usage.get("prompt", 0) / 1_000_000 * rate[0]
    out = (usage.get("output", 0) + usage.get("thoughts", 0)) / 1_000_000 * rate[1]
    usd = inp + out
    return {
        "usd": round(usd, 6),
        "krw": round(usd * USD_KRW, 1),
        "rate_in": rate[0], "rate_out": rate[1],
        "exact": exact,
        "basis": ("공개 단가 기준" if exact else "같은 등급 단가로 추정"),
    }


def gemini_json(system: str, prompt: str, model: str = "", key: str = "", timeout: int = 180,
                usage=None, temperature: float = 0.25):
    settings = load_settings()
    key = key or settings.get("api_key") or ""
    model = model or settings.get("model") or DEFAULT_MODEL
    if not key:
        raise AiError("Gemini API 키가 설정되지 않았습니다. 오른쪽 위 설정에서 키를 입력해 주세요.")
    url = "%s/models/%s:generateContent" % (GEMINI_BASE, urllib.parse.quote(model))
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }
    data = _request(url, payload, key=key, timeout=timeout)
    _collect_usage(data, usage)
    return _parse_json(_extract_text(data))


# --------------------------------------------------------------------------- 로컬 검색
def _norm_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def cached_keywords(query: str):
    """같은 질문이면 같은 검색어를 쓰도록 캐시한다.

    Gemini 는 temperature 0 에서도 매번 조금씩 다른 키워드를 내놓아서
    같은 질문인데 후보가 달라지는 원인이 된다. 캐시로 결과를 고정한다.
    """
    if not os.path.exists(KEYWORD_CACHE_PATH):
        return None
    try:
        with open(KEYWORD_CACHE_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get(_norm_query(query))
    except Exception:
        return None


def store_keywords(query: str, keywords, fields, goal: str = "") -> None:
    with FILE_LOCK:
        data = {}
        if os.path.exists(KEYWORD_CACHE_PATH):
            try:
                with open(KEYWORD_CACHE_PATH, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        data[_norm_query(query)] = {"keywords": list(keywords), "fields": list(fields),
                                    "goal": goal, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if len(data) > 500:  # 오래된 것부터 정리
            for key in sorted(data, key=lambda k: data[k].get("at", ""))[:len(data) - 500]:
                data.pop(key, None)
        os.makedirs(os.path.dirname(KEYWORD_CACHE_PATH), exist_ok=True)
        tmp = KEYWORD_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, KEYWORD_CACHE_PATH)


def strip_particle(token: str) -> str:
    """'입지를' → '입지', '위치와' → '위치' 처럼 붙은 조사를 떼어 낸다."""
    for particle in PARTICLES:
        if len(token) > len(particle) + 1 and token.endswith(particle):
            return token[:-len(particle)]
    return token


def tokenize(text: str) -> list:
    raw = re.split(r"[^0-9A-Za-z가-힣]+", text or "")
    out = []
    for token in raw:
        token = strip_particle(token.strip())
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in out:
            out.append(token)
    return out[:12]


# 검색 대상 필드와 가중치 (SQL 안에서 한 번에 점수를 계산한다)
SEARCH_FIELDS = (("i.title", 6.0), ("i.file_name", 3.0), ("i.keywords_json", 3.0),
                 ("c.columns_text", 2.5), ("i.description", 1.5))
SCAN_CAP = 30000        # 점수 계산에 올릴 최대 행 수
GLOBAL_TOP = 220        # 종합 점수 상위
PER_KEYWORD_TOP = 30    # 키워드마다 반드시 확보할 상위 건수


def expand_keywords(keywords, query: str = "") -> list:
    """Gemini 키워드를 우선하고, 모자란 만큼만 질문 토큰으로 채운다.

    질문 토큰은 조사·서술어가 섞이기 쉬워 검색을 흐리므로 보조로만 쓴다.
    """
    out = []

    def add(word):
        word = str(word).strip()
        if len(word) < 2 or word in STOPWORDS or word in out:
            return
        out.append(word)
        nospace = word.replace(" ", "")
        if nospace != word and len(nospace) >= 2 and nospace not in out:
            out.append(nospace)

    for kw in (keywords or []):
        add(kw)
    primary = len(out)
    for token in tokenize(query):          # 질문 토큰은 최대 4개만 보충
        if len(out) >= primary + 6 or len(out) >= 24:
            break
        add(token)
    return out[:24]


FTS_FIELDS = (("title_text", 6.0), ("meta_text", 3.0), ("desc_text", 1.5))
FTS_LIMIT = 6000          # 키워드·필드마다 가져올 최대 uid 수


def _fts_hits(con, keywords):
    """키워드별 {uid: 필드가중치합} 을 구한다.

    3글자 이상은 FTS5(trigram) 색인으로 즉시 찾고, 2글자는 trigram 으로 표현할 수
    없으므로 슬림 테이블을 한 번만 스캔해 한꺼번에 처리한다.
    """
    hits = [dict() for _ in keywords]
    short = [(i, kw) for i, kw in enumerate(keywords) if len(kw) < 3]
    for i, kw in enumerate(keywords):
        if len(kw) < 3:
            continue
        safe = kw.replace('"', " ").strip()
        if not safe:
            continue
        for col, weight in FTS_FIELDS:
            try:
                rows = con.execute(
                    "SELECT uid FROM search_fts WHERE search_fts MATCH ? LIMIT ?",
                    ('%s : "%s"' % (col, safe), FTS_LIMIT)).fetchall()
            except Exception:
                rows = []
            bucket = hits[i]
            for (uid,) in rows:
                bucket[uid] = bucket.get(uid, 0.0) + weight
    if short:
        exprs, args = [], []
        for _, kw in short:
            like = "%" + kw + "%"
            exprs.append("(CASE WHEN title_text LIKE ? THEN 6.0 ELSE 0 END)"
                         "+(CASE WHEN meta_text LIKE ? THEN 3.0 ELSE 0 END)"
                         "+(CASE WHEN desc_text LIKE ? THEN 1.5 ELSE 0 END)")
            args.extend([like] * 3)
        names = ["s%d" % j for j in range(len(short))]
        inner = ",".join("%s %s" % (e, n) for e, n in zip(exprs, names))
        total = "+".join(names)
        sql = ("WITH m AS (SELECT uid,%s FROM item_search) SELECT uid,%s FROM m WHERE (%s)>0"
               % (inner, ",".join(names), total))
        for row in con.execute(sql, args):
            for j, (idx, _kw) in enumerate(short):
                score = row[names[j]]
                if score:
                    hits[idx][row["uid"]] = score
    return hits


def _scan_scores(con, keywords, cap: int = SCAN_CAP):
    """FTS 색인이 없을 때 쓰는 예비 경로(전체 스캔)."""
    parts, args = [], []
    for kw in keywords:
        like = "%" + kw + "%"
        exprs = []
        for col, weight in SEARCH_FIELDS:
            exprs.append("(CASE WHEN %s LIKE ? THEN %s ELSE 0 END)" % (col, weight))
            args.append(like)
        parts.append("(" + "+".join(exprs) + ")")
    names = ["k%d" % i for i in range(len(parts))]
    inner = ",".join("%s %s" % (expr, name) for expr, name in zip(parts, names))
    total = "+".join(names)
    sql = (
        "WITH m AS (SELECT i.uid uid,%s FROM catalog_items i "
        "LEFT JOIN item_columns c ON c.uid=i.uid WHERE i.active=1) "
        "SELECT uid,%s,(%s) total FROM m WHERE (%s)>0 ORDER BY total DESC LIMIT ?"
        % (inner, ",".join(names), total, total)
    )
    args.append(cap)
    rows = con.execute(sql, args).fetchall()
    hits = [dict() for _ in keywords]
    for row in rows:
        for i, name in enumerate(names):
            if row[name]:
                hits[i][row["uid"]] = row[name]
    return hits


def _fetch_rows(con, uids):
    """고른 후보만 상세 정보를 읽어 온다."""
    out = []
    for i in range(0, len(uids), 400):
        chunk = uids[i:i + 400]
        sql = (
            "SELECT i.uid,i.source,i.source_id,i.title,i.file_name,i.field,i.subfield,i.organization,"
            "i.formats_json,i.keywords_json,substr(i.description,1,600) description,i.modified_at,"
            "i.update_cycle,i.row_count,i.views,i.downloads,i.url,i.media_type,i.preview_status,"
            "json_extract(i.detail_json,'$.delivery') delivery,"
            "json_extract(i.detail_json,'$.extension') extension,"
            "c.columns_json,c.n column_count "
            "FROM catalog_items i LEFT JOIN item_columns c ON c.uid=i.uid "
            "WHERE i.uid IN (%s)" % ",".join("?" * len(chunk))
        )
        out.extend(con.execute(sql, chunk).fetchall())
    return out


def _rows_for(con, keywords, use_description: bool, limit_rows: int):
    if not keywords:
        return []
    cols = ["i.title", "i.file_name", "i.keywords_json", "c.columns_text"]
    if use_description:
        cols.append("i.description")
    clauses, args = [], []
    for kw in keywords:
        like = "%" + kw + "%"
        clauses.append("(" + " OR ".join("%s LIKE ?" % x for x in cols) + ")")
        args.extend([like] * len(cols))
    sql = (
        "SELECT i.uid,i.source,i.source_id,i.title,i.file_name,i.field,i.subfield,i.organization,"
        "i.formats_json,i.keywords_json,substr(i.description,1,600) description,i.modified_at,"
        "i.update_cycle,i.row_count,i.views,i.downloads,i.url,i.media_type,i.preview_status,"
        "json_extract(i.detail_json,'$.delivery') delivery,"
        "json_extract(i.detail_json,'$.extension') extension,"
        "c.columns_json,c.n column_count "
        "FROM catalog_items i LEFT JOIN item_columns c ON c.uid=i.uid "
        "WHERE i.active=1 AND (" + " OR ".join(clauses) + ") "
        "ORDER BY i.downloads DESC, i.views DESC LIMIT ?"
    )
    args.append(limit_rows)
    return con.execute(sql, args).fetchall()


def search_candidates(keywords, fields=None, limit: int = 60, query: str = "") -> list:
    """질문에 맞는 후보를 폭넓게 찾는다.

    - 3글자 이상 키워드는 FTS5(trigram) 색인으로 즉시 조회하므로 키워드를 많이 써도 빠르다.
    - 키워드마다 상위 N건을 반드시 확보해 특정 개념이 통째로 빠지지 않게 한다.
    - 흔한 낱말은 IDF 로 가중치를 낮춰 정밀도를 지킨다.
    """
    keywords = expand_keywords(keywords, query)
    if not keywords:
        return []
    con = connect(DB_PATH, readonly=True)
    try:
        use_fts = has_fts(con)
        hits = _fts_hits(con, keywords) if use_fts else _scan_scores(con, keywords)
        matched = set()
        for bucket in hits:
            matched.update(bucket)
        if not matched:
            return []

        # 흔한 낱말일수록 가중치를 낮춘다
        total_rows = max(1, len(matched))
        idf = [max(0.35, math.log((total_rows + 1) / (len(b) + 1)) / math.log(20) + 0.35) for b in hits]

        combined = {}
        for i, bucket in enumerate(hits):
            weight = idf[i]
            for uid, raw in bucket.items():
                slot = combined.setdefault(uid, [0.0, 0])
                slot[0] += raw * weight
                slot[1] += 1
        ranked = sorted(combined.items(), key=lambda kv: -(kv[1][0] + kv[1][1] * 3.0))

        chosen, seen = [], set()
        for uid, _ in ranked[:GLOBAL_TOP]:
            seen.add(uid)
            chosen.append(uid)
        # 키워드마다 상위 건을 반드시 포함시킨다
        for bucket in hits:
            for uid, _raw in sorted(bucket.items(), key=lambda kv: -kv[1])[:PER_KEYWORD_TOP]:
                if uid not in seen:
                    seen.add(uid)
                    chosen.append(uid)

        rows = _fetch_rows(con, chosen)
        stats = {"matched": len(matched), "scanned_pool": len(chosen), "keywords": keywords,
                 "keyword_hits": {kw: len(b) for kw, b in zip(keywords, hits)},
                 "engine": "fts" if use_fts else "scan", "capped": False}
    finally:
        con.close()

    by_uid = {r["uid"]: r for r in rows}
    fields = set(fields or [])
    scored = []
    for uid in chosen:
        row = by_uid.get(uid)
        if row is None:
            continue
        base, covered = combined.get(uid, [0.0, 0])
        if not covered:
            continue
        score = base + covered * 4
        if row["field"] in fields:
            score += 5
        score += min(4.0, (row["downloads"] or 0) ** 0.35 / 6)
        if row["column_count"]:
            score += 2  # 데이터 항목을 아는 데이터가 실제 활용 판단에 유리하다
        if row["source"] == "AI Hub":
            score += 1.5  # AI 학습용 데이터는 목록이 적어 노출 기회를 보정
        scored.append((score, dict(row), covered))

    scored.sort(key=lambda x: -x[0])
    quota_ai = max(8, limit // 3)
    picked, ai_n = [], 0
    for score, row, covered in scored:
        if len(picked) >= limit:
            break
        if row["source"] == "AI Hub":
            if ai_n >= quota_ai and len(picked) > limit * 0.6:
                continue
            ai_n += 1
        row["_score"] = round(score, 2)
        row["_covered"] = covered
        picked.append(row)
    if picked:
        picked[0]["_stats"] = stats
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
  "keywords": ["검색 키워드 16~22개, 중요한 것부터"],
  "fields": ["다음 분야명 중 관련된 것만: {fields}"]
}}

키워드 규칙:
- 목적을 이루는 데 필요한 서로 다른 개념을 빠짐없이 넣는다(대상·현상·장소·시설·지표 등).
- 검색은 매우 빠르므로 넉넉하게 넣는다. 놓치는 것보다 조금 넓게 잡는 편이 낫다.
- 같은 뜻의 다른 표기를 반드시 함께 넣는다. 예) 전기차 / 전기자동차 / 친환경차,
  충전소 / 충전기 / 충전시설, 정신건강 / 심리 / 상담 / 정신질환,
  인구 / 유동인구 / 주민등록 / 세대수, 침수 / 홍수 / 강우 / 하천수위.
- 붙여 쓴 형태와 띄어 쓴 형태가 다르면 둘 다 넣는다. 예) 전기차충전소 / 전기차 충전소.
- 데이터가 담길 만한 기관·제도 이름도 넣는다. 예) 정신건강복지센터, 청소년상담복지센터.
- 카탈로그의 데이터명이나 컬럼명에 실제로 등장할 법한 2~6글자 명사로 쓴다.
- 조사·서술어('하는', '위한')와 '데이터', '정보' 같은 일반어는 넣지 않는다."""

# 안심존(AI Hub 보건의료 등) 이용 규칙 요약 - docs/안심존 사용 방법.md 기반.
# 안심존 데이터가 후보에 있을 때만 프롬프트에 붙인다.
SAFEZONE_GUIDE = """[안심존 데이터 이용 규칙 - 추천에 반드시 반영]
- 안심존은 데이터를 받아오는 곳이 아니라, 데이터가 있는 서버에 내 개발환경을 가져가
  학습시키고 결과물만 들고 나오는 곳이다.
- 밖으로 반출 가능: 소스코드, 학습된 AI 모델 파일(model.pkl/pt/h5)뿐.
  반출 불가: 원본·가공 데이터, CSV/NPY/PKL 데이터, 이미지·영상·음성·압축파일.
  성능 결과도 간단한 그래프 수준만 심사를 거쳐 나올 수 있다.
- 신청: 열람이 아니라 '데이터 사용' 신청이며 IRB 승인과 연구계획서가 필요하다.
  여러 데이터를 조합할 계획이면 처음부터 '멀티데이터'로 함께 신청해야 한다.
- 서버: IRB 승인 연구 1건당 GPU 서버 1대, 사용기간 30일(주말·공휴일 포함).
  V100 32GB, 8 vCPU, RAM 90GB, Ubuntu 22.04, CUDA 12.2, Python 3.11 수준.
- 인터넷은 초기 최대 5일만 열린다. 그 사이에 패키지 설치·코드 업로드·자체 데이터 업로드를
  끝내야 하고, 데이터를 마운트하면 인터넷이 차단된다. 순서를 어기면 서버 초기화(약 1일)가 필요하다.
- 그래서 학습 코드는 밖에서 동일 스키마의 샘플·공개 데이터로 미리 완성해 두고 들어가야 한다.
- 원본 데이터 폴더는 읽기 전용이라 작업 폴더(nasw)로 복사해 쓴다. SHAP 등 설명가능성 분석도 내부에서 수행한다.
- 모델 파일에 학습 데이터가 들어가면 반출이 막힌다. 모델·feature_names·threshold·버전만 저장한다.
- 반출은 nasw/download 에 넣고 종료일 기준 최소 영업일 3일 전에 신청한다(심사 2~3영업일, 최대 3회).
"""

SAFEZONE_SCHEMA = """,
  "safezone_plan": {{
    "datasets": ["안심존으로만 쓸 수 있는 추천 데이터의 uid"],
    "outside": ["안심존에 들어가기 전에 밖에서 준비할 일 3~5개. 어떤 공개 데이터로 코드를 미리 검증할지 포함"],
    "inside": ["안심존 안에서 순서대로 할 일 4~6개. 인터넷이 열려 있는 초기 5일에 할 일과 마운트 이후 할 일을 구분"],
    "combine": "다운로드 가능한 데이터와 안심존 데이터의 역할을 어떻게 나눌지 2~3문장",
    "export": ["30일 뒤 반출할 결과물. 반출 가능한 것만 적는다"],
    "schedule": [{{"period": "Day 1~5", "task": "그 기간에 할 일"}}],
    "cautions": ["안심존 때문에 특별히 조심할 점 2~4개"]
  }}"""

RECO_SYSTEM = (
    "너는 데이터 기반 서비스 기획자다. 사용자의 목적과 '후보 데이터 목록'을 보고, "
    "목적 달성에 필요한 기능을 정의하고 후보 중에서 실제로 쓸 데이터를 골라 활용 방법을 제시한다. "
    "각 후보에는 실제 '데이터항목'(컬럼 이름)이 함께 주어진다. 데이터명이 그럴듯해도 "
    "데이터항목에 목적에 필요한 값이 없으면 고르지 말고, 항목이 목적과 맞는 데이터를 우선한다. "
    "반드시 후보 목록에 있는 uid만 사용하고, 목록에 없는 데이터를 지어내지 않는다. "
    "모든 설명은 한국어로 쓰고 JSON만 출력한다."
)
RECO_PROMPT = """[사용자 목적]
\"\"\"{query}\"\"\"

[후보 데이터 목록]
{candidates}
{safezone_guide}
위 후보만 사용해 아래 JSON 형식으로 답하라.
{{
  "title": "이 추천을 나타내는 12자 이내 제목",
  "goal": "목적을 1~2문장으로 정리",
  "features": [
    {{"name": "필요한 기능 이름", "detail": "무엇을 하는 기능인지 한두 문장", "data_need": "이 기능에 필요한 데이터 항목"}}
  ],
  "datasets": [
    {{"uid": "후보 목록의 uid", "role": "핵심|보조|참고",
      "why": "이 목적에 왜 필요한지 한두 문장. 어떤 데이터항목 때문에 쓸 만한지 근거를 넣는다",
      "usage": "구체적인 활용 방법. 어떤 항목을 어떻게 가공/결합하는지 적는다",
      "items": ["실제로 쓸 데이터항목 3~6개. 후보의 '데이터항목'에 있는 이름을 그대로 적는다"],
      "join_key": "다른 데이터와 연결할 때 쓸 공통 항목(없으면 빈 문자열)"}}
  ],
  "pipeline": [
    {{"step": "단계 이름(6자 내외)",
      "uses": ["이 단계에서 쓰는 데이터의 uid (후보 목록에 있는 것만, 없으면 빈 배열)"],
      "detail": "이 데이터의 어떤 항목을 어떻게 쓰는지 2~3문장으로 구체적으로. 결합·가공 방법과 기준을 적는다",
      "output": "이 단계가 만들어 내는 결과물(표·지표·모델 등)을 한 문장으로"}}
  ],
  "outcome": "위 흐름을 끝냈을 때 실제로 무엇을 할 수 있게 되는지 2~3문장. 어떤 질문에 답할 수 있고 무엇을 만들 수 있는지 구체적으로",
  "cautions": ["데이터 활용 시 유의사항 2~4개"],
  "missing": ["후보에 없어서 추가로 확보해야 할 데이터 1~3개"]{safezone_schema}
}}

규칙:
- datasets 는 중요도 순으로 최대 15개. '핵심'은 3~5개로 제한하되,
  목적에 조금이라도 쓸모 있는 후보는 빠뜨리지 말고 '보조'나 '참고'로라도 넣는다.
  같은 성격의 데이터가 지역만 다르게 여러 건이면 대표적인 것들을 함께 넣어 준다.
- items 는 반드시 해당 후보의 '데이터항목'에 실제로 있는 이름만 쓴다.
  항목 정보가 없는 후보(항목 정보 없음)는 items 를 비우고 why 에 "항목 미확인"이라고 적는다.
- 데이터항목을 보고 목적에 쓸 값이 없다고 판단되면 그 후보는 아예 고르지 않는다.
- features 의 data_need 도 가능하면 후보들의 실제 데이터항목 이름으로 적는다.
- pipeline 의 detail 은 "A 데이터의 X 항목과 B 데이터의 Y 항목을 Z 기준으로 결합한다" 처럼
  실제 데이터명과 항목명을 넣어 쓴다. 일반론("전처리한다", "모델을 학습한다")만 쓰지 않는다.
- 설명 문장에서는 uid 대신 사람이 읽는 데이터명을 쓴다. uid 는 uses 와 datasets 에만 넣는다.
- pipeline 의 uses 에는 datasets 에 넣은 uid 만 쓴다.
- features 는 3~6개, pipeline 은 4~6단계로 만든다.
- 후보에 마땅한 데이터가 없으면 datasets 를 비우고 missing 에 이유를 적는다.{safezone_rules}"""

SAFEZONE_RULES = """
- 제공방식이 '안심존'인 데이터를 추천했다면 safezone_plan 을 반드시 채운다.
  하나도 추천하지 않았다면 safezone_plan 은 넣지 않는다.
- 안심존 데이터가 섞였다면 pipeline 에도 그 사실을 반영한다. 즉 안심존 데이터를 쓰는 단계는
  '안심존 서버 안에서' 수행하고, 그 결과로 데이터가 아니라 모델·코드만 나온다는 점을 적는다.
- 다운로드 가능한 데이터는 밖에서 미리 코드·전처리를 검증하는 용도로 배치해,
  안심존 30일을 낭비하지 않도록 설계한다."""


UID_RE = re.compile(r"(?:aihub|public):[0-9]+")


def humanize(text: str, by_uid: dict) -> str:
    """설명 문장에 남은 내부 uid 를 사람이 읽는 데이터명으로 바꾼다."""
    if not text:
        return ""
    def swap(m):
        title = (by_uid.get(m.group(0)) or {}).get("title")
        return "'%s'" % title if title else m.group(0)
    out = UID_RE.sub(swap, text)
    # 모델이 "이름(uid)" 로 쓴 경우 치환 후 이름이 두 번 나오므로 하나로 줄인다.
    for row in by_uid.values():
        title = (row or {}).get("title")
        if not title or title not in out:
            continue
        quoted = "'%s'" % title
        for dup in ("%s(%s)" % (title, quoted), "%s (%s)" % (title, quoted),
                    "%s(%s)" % (quoted, quoted), "%s (%s)" % (quoted, quoted)):
            out = out.replace(dup, quoted)
        out = out.replace("'%s'" % quoted, quoted)  # 따옴표가 겹친 경우
    return out


def columns_of(row) -> list:
    return decode_json(row.get("columns_json") if isinstance(row, dict) else row["columns_json"], []) or []


def _candidate_block(rows, aihub_access) -> str:
    lines = []
    for row in rows:
        acc = access_of(row, aihub_access)
        formats = ", ".join(decode_json(row.get("formats_json"), []) or [])
        kws = ", ".join((decode_json(row.get("keywords_json"), []) or [])[:6])
        desc = re.sub(r"\s+", " ", (row.get("description") or ""))[:200]
        cols = columns_of(row)
        if cols:
            shown = ", ".join(cols[:18])
            col_text = "%s%s (총 %d개)" % (shown[:320], " …" if len(cols) > 18 else "", len(cols))
        else:
            col_text = "(항목 정보 없음)"
        lines.append(
            "- uid: {uid} | 출처: {source} | 데이터명: {title} | 분야: {field}{sub} | 제공기관: {org} | "
            "형식: {formats} | 제공방식: {acc} | 행수: {rows} | 갱신: {mod}\n"
            "  데이터항목: {cols}\n"
            "  키워드: {kws} | 설명: {desc}".format(
                uid=row["uid"], source=row["source"], title=row["title"], field=row.get("field") or "-",
                sub=(" > " + row["subfield"]) if row.get("subfield") else "", org=row.get("organization") or "-",
                formats=formats or "-", acc=acc["type"], rows=row.get("row_count") or "-",
                mod=row.get("modified_at") or "-", cols=col_text, kws=kws or "-", desc=desc or "-")
        )
    return "\n".join(lines)


def _safezone_plan(result: dict, by_uid: dict, datasets: list):
    """안심존 계획을 정리한다. 실제로 안심존 데이터를 추천했을 때만 남긴다."""
    locked = [d for d in datasets if (d.get("access") or {}).get("type") == "안심존"]
    if not locked:
        return None
    plan = result.get("safezone_plan") or {}
    text = lambda v: humanize(str(v or ""), by_uid)
    items = lambda key, n: [text(x) for x in (plan.get(key) or [])][:n]
    return {
        "datasets": [{"uid": d["uid"], "title": d["title"], "source": d["source"]} for d in locked],
        "outside": items("outside", 6),
        "inside": items("inside", 7),
        "combine": text(plan.get("combine")),
        "export": items("export", 5),
        "schedule": [{"period": str(s.get("period") or ""), "task": text(s.get("task"))}
                     for s in (plan.get("schedule") or [])][:8],
        "cautions": items("cautions", 5),
    }


def recommend(query: str, model: str = "", aihub_access: dict | None = None, fields_available=None) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        raise AiError("찾고 싶은 데이터를 조금 더 자세히 적어 주세요.")
    aihub_access = aihub_access or {}
    started = time.time()
    settings = load_settings()
    model = model or settings.get("model") or DEFAULT_MODEL
    usage = {}

    # 1단계 - 검색 키워드. 같은 질문은 캐시된 키워드를 재사용해 결과가 흔들리지 않게 한다.
    fields_available = fields_available or []
    cached = cached_keywords(query)
    plan = {}
    if cached and cached.get("keywords"):
        keywords = list(cached["keywords"])
        fields = [f for f in (cached.get("fields") or []) if f in set(fields_available)]
        plan = {"goal": cached.get("goal", "")}
        from_cache = True
    else:
        plan = gemini_json(
            KEYWORD_SYSTEM,
            KEYWORD_PROMPT.format(query=query, fields=", ".join(fields_available[:40])),
            model=model, timeout=90, usage=usage, temperature=0.0,
        )
        keywords = [str(k).strip() for k in (plan.get("keywords") or []) if str(k).strip()][:22]
        keywords = [k for k in keywords if k not in STOPWORDS]
        if not keywords:
            keywords = tokenize(query)
        fields = [f for f in (plan.get("fields") or []) if f in set(fields_available)]
        store_keywords(query, keywords, fields, str(plan.get("goal") or ""))
        from_cache = False

    # 2단계 - 로컬 카탈로그 검색 (질문 원문 토큰도 함께 넣어 놓치는 데이터를 줄인다)
    candidates = search_candidates(keywords, fields, limit=60, query=query)
    if not candidates:
        candidates = search_candidates(tokenize(query), [], limit=60, query=query)
    if not candidates:
        raise AiError("카탈로그에서 관련 데이터를 찾지 못했습니다. 다른 표현으로 다시 검색해 보세요. (검색어: %s)" % ", ".join(keywords))

    # 3단계 - 추천 생성. 안심존 후보가 섞여 있을 때만 안심존 이용 규칙을 함께 준다.
    has_safezone = any(access_of(row, aihub_access)["type"] == "안심존" for row in candidates)
    result = gemini_json(
        RECO_SYSTEM,
        RECO_PROMPT.format(
            query=query,
            candidates=_candidate_block(candidates, aihub_access),
            safezone_guide=("\n" + SAFEZONE_GUIDE) if has_safezone else "",
            safezone_schema=SAFEZONE_SCHEMA if has_safezone else "",
            safezone_rules=SAFEZONE_RULES if has_safezone else "",
        ),
        model=model, timeout=240, usage=usage, temperature=0.1,  # 같은 질문에 결과가 흔들리지 않게
    )

    by_uid = {row["uid"]: row for row in candidates}
    datasets, dropped = [], 0
    for item in (result.get("datasets") or [])[:16]:
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
            "why": humanize(str(item.get("why") or ""), by_uid),
            "usage": humanize(str(item.get("usage") or ""), by_uid),
            "items": [str(x) for x in (item.get("items") or [])][:8],
            "join_key": str(item.get("join_key") or ""),
            "columns": columns_of(row)[:40],
            "column_count": row.get("column_count") or 0,
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
        "features": [{"name": str(f.get("name") or ""),
                      "detail": humanize(str(f.get("detail") or ""), by_uid),
                      "data_need": humanize(str(f.get("data_need") or ""), by_uid)}
                     for f in (result.get("features") or [])][:8],
        "datasets": datasets,
        "pipeline": [{
            "step": str(p.get("step") or ""),
            "detail": humanize(str(p.get("detail") or ""), by_uid),
            "output": humanize(str(p.get("output") or ""), by_uid),
            # 흐름에서 쓰는 데이터는 실제 추천 목록에 있는 것만 남기고 제목을 붙여 준다
            "uses": [{"uid": u, "title": (by_uid.get(u) or {}).get("title", ""),
                      "source": (by_uid.get(u) or {}).get("source", "")}
                     for u in (p.get("uses") or []) if u in by_uid][:6],
        } for p in (result.get("pipeline") or [])][:8],
        "outcome": humanize(str(result.get("outcome") or ""), by_uid),
        "cautions": [str(c) for c in (result.get("cautions") or [])][:6],
        "missing": [str(m) for m in (result.get("missing") or [])][:5],
        "safezone": _safezone_plan(result, by_uid, datasets),
        "candidate_count": len(candidates),
        "search": dict(candidates[0].get("_stats") or {}, cached=from_cache) if candidates else None,
        "manual_url": SAFEZONE_MANUAL_URL,
        "dropped": dropped,
        "elapsed": round(time.time() - started, 1),
        "usage": dict(usage, cost=estimate_cost(model, usage)) if usage else None,
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
