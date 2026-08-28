# -*- coding: utf-8 -*-
"""Cloudflare Pages 용 정적 사이트 빌드.

data/snapshot/*.jsonl.gz (깃에 포함) 를 읽어 브라우저가 바로 쓸 수 있는
JSON 샤드로 나눈다. SQLite 복원 없이 표준 라이브러리만으로 동작하므로
Cloudflare Pages 빌드 컨테이너에서도 그대로 돌아간다.

  python build_static.py            # -> site/
  python build_static.py --out dist

산출물
  site/index.html                기존 대시보드(정적 모드로 동작)
  site/static-api.js             /api/* 를 정적 샤드로 대체하는 데이터 계층
  site/data/meta.json            통계·수집시각·분야/형식 집계
  site/data/idx-NN.json          검색 색인(제목·기관·키워드·컬럼명·설명 앞부분)
  site/data/page-NNNN.json       기본 정렬(수정일 내림차순) 100건씩 - 첫 화면용
  site/data/det-NNNN.json        상세 100건씩(상세정보 + 미리보기 전체)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshot")
DETAILS_DIR = os.path.join(DATA_DIR, "details")
AIHUB_JSON = os.path.join(DATA_DIR, "datasets.json")

PAGE_SIZE = 100          # 목록 한 페이지
FIRST_PAGES = 20         # 첫 화면용으로 미리 만들 페이지 수(그 뒤는 색인으로 처리)
DETAIL_SHARD = 100       # 상세 샤드당 건수
INDEX_SHARDS = 12        # 검색 색인 분할 수
DESC_IN_INDEX = 160      # 색인에 넣을 설명 길이
# 산출물은 gzip 으로 저장하고 _headers 에서 Content-Encoding 을 지정한다.
# 브라우저가 알아서 풀기 때문에 데이터를 하나도 줄이지 않고 용량만 1/6 로 줄일 수 있다.


def log(msg):
    print("[build] %s" % msg, flush=True)


def load_aihub_access():
    """AI Hub 제공방식(다운로드/안심존)·용량 정보."""
    table = {}
    if not os.path.exists(AIHUB_JSON):
        return table
    with open(AIHUB_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    for item in payload.get("datasets", []):
        sn = str(item.get("sn") or "")
        if sn:
            table[sn] = {
                "status": item.get("status") or "",
                "approval_required": bool(item.get("approval_required")),
                "offline_available": bool(item.get("offline_available")),
                "has_sample": bool(item.get("has_sample")),
                "size_bytes": int(item.get("size_bytes") or 0),
                "s3_file_cnt": int(item.get("s3_file_cnt") or 0),
            }
    return table


def access_of(source, source_id, detail, aihub):
    """serve.py 의 ai_service.access_of 와 같은 규칙."""
    if source == "AI Hub":
        info = aihub.get(str(source_id)) or {}
        status = info.get("status") or ""
        size = info.get("size_bytes", 0)
        if status == "안심존":
            return {"type": "안심존", "tone": "lock", "note": "이용신청 후 열람만 가능", "size_bytes": size}
        if status == "준비중":
            return {"type": "준비중", "tone": "wait", "note": "", "size_bytes": size}
        if status == "데이터 있음":
            return {"type": "다운로드", "tone": "download",
                    "note": "개별 승인 필요" if info.get("approval_required") else "", "size_bytes": size}
        return {"type": "확인 필요", "tone": "muted", "note": "", "size_bytes": size}
    delivery = str((detail or {}).get("delivery") or "")
    ext = str((detail or {}).get("extension") or "").upper()
    if "기관자체" in delivery or "URL" in delivery.upper():
        return {"type": "기관 제공", "tone": "wait", "note": "제공기관 사이트에서 다운로드", "size_bytes": 0}
    if "전자기록매체" in delivery:
        return {"type": "오프라인", "tone": "lock", "note": "전자기록매체 저장 제공", "size_bytes": 0}
    return {"type": "다운로드", "tone": "download", "note": ("%s 파일" % ext) if ext else "", "size_bytes": 0}


def aihub_detail(source_id):
    """AI Hub 상세(섹션 HTML 등)."""
    path = os.path.join(DETAILS_DIR, "%s.json" % source_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def aihub_summary_map():
    """AI Hub 목록 원본에서 상세 화면에 쓰는 요약 필드."""
    out = {}
    if not os.path.exists(AIHUB_JSON):
        return out
    keep = ("intro", "purpose", "meta", "build_year", "update_ym", "build_amount", "data_format",
            "label_type", "label_format", "data_source", "use_service", "builder_main", "builder_sub",
            "gen_method", "types", "tags", "size_bytes", "status", "approval_required",
            "offline_available", "has_sample", "has_manual", "has_guide", "s3_file_cnt", "notice")
    with open(AIHUB_JSON, encoding="utf-8") as f:
        for item in json.load(f).get("datasets", []):
            out[str(item.get("sn"))] = {k: item.get(k) for k in keep if item.get(k) not in (None, "")}
    return out


def read_snapshot():
    """스냅샷 JSONL 을 순서대로 읽는다."""
    manifest_path = os.path.join(SNAP_DIR, "manifest.json")
    if not os.path.exists(manifest_path):
        sys.exit("스냅샷이 없습니다: %s\n먼저 python snapshot.py export 를 실행하세요." % manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    for part in manifest.get("parts", []):
        path = os.path.join(SNAP_DIR, part)
        if not os.path.exists(path):
            log("파트 없음: %s" % part)
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    return


def write_json(path, obj):
    """gzip 으로 저장한다(.json.gz). Content-Encoding 헤더로 브라우저가 바로 읽는다."""
    path = path + ".gz"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(path, "wb", compresslevel=6, mtime=0) as f:
        f.write(raw)


def build(out_dir):
    started = time.time()
    aihub = load_aihub_access()
    summaries = aihub_summary_map()
    data_out = os.path.join(out_dir, "data")
    if os.path.exists(data_out):
        shutil.rmtree(data_out)
    os.makedirs(data_out, exist_ok=True)

    items = []          # 목록/검색용 경량 레코드
    detail_buf = []     # 상세 샤드 버퍼
    detail_no = 0
    field_count = Counter()
    format_count = Counter()
    source_count = Counter()
    preview_count = Counter()
    access_count = Counter()
    stamps = []

    log("스냅샷 읽는 중…")
    for row in read_snapshot():
        uid = row["uid"]
        source = row["source"]
        detail = row.get("detail") or {}
        preview = row.get("preview") or {}
        formats = json.loads(row.get("formats_json") or "[]")
        keywords = json.loads(row.get("keywords_json") or "[]")
        columns = list((preview.get("headers") or (detail.get("columns") or {}).get("headers") or []))
        acc = access_of(source, row["source_id"], detail, aihub)

        source_count[source] += 1
        if row.get("field"):
            field_count[row["field"]] += 1
        for fmt in formats:
            format_count[str(fmt).upper()] += 1
        preview_count[row.get("preview_status") or ""] += 1
        access_count[acc["type"]] += 1
        if row.get("crawled_at"):
            stamps.append(row["crawled_at"])

        # 검색 색인 텍스트: 키워드 + 컬럼명 (제목·기관은 별도 필드)
        meta_text = " ".join([str(k) for k in keywords] + [str(c) for c in columns])
        items.append({
            "uid": uid, "s": 1 if source == "AI Hub" else 0, "t": row.get("title") or "",
            "f": row.get("field") or "", "sf": row.get("subfield") or "",
            "o": row.get("organization") or "", "fm": formats,
            "m": row.get("modified_at") or "", "c": row.get("created_at") or "",
            "rc": row.get("row_count") or 0, "dl": row.get("downloads") or 0,
            "vw": row.get("views") or 0, "u": row.get("url") or "",
            "a": acc, "cn": len(columns), "ps": row.get("preview_status") or "",
            "mt": meta_text[:600], "d": (row.get("description") or "")[:DESC_IN_INDEX],
            "uc": row.get("update_cycle") or "", "sid": row["source_id"],
        })

        # 상세 레코드
        record = {
            "uid": uid, "source": source, "source_id": row["source_id"],
            "title": row.get("title") or "", "file_name": row.get("file_name") or "",
            "field": row.get("field") or "", "subfield": row.get("subfield") or "",
            "organization": row.get("organization") or "",
            "organization_type": row.get("organization_type") or "",
            "formats": formats, "keywords": keywords,
            "description": row.get("description") or "",
            "modified_at": row.get("modified_at") or "", "created_at": row.get("created_at") or "",
            "next_update": row.get("next_update") or "", "update_cycle": row.get("update_cycle") or "",
            "media_type": row.get("media_type") or "", "row_count": row.get("row_count") or 0,
            "views": row.get("views") or 0, "downloads": row.get("downloads") or 0,
            "url": row.get("url") or "", "detail_status": row.get("detail_status") or "",
            "preview_status": row.get("preview_status") or "", "crawled_at": row.get("crawled_at") or "",
            "access": acc, "columns": columns, "column_count": len(columns),
            "detail": detail, "preview": preview, "error": row.get("error") or "",
        }
        if source == "AI Hub":
            record["detail"] = aihub_detail(row["source_id"]) or detail
            record["aihub"] = summaries.get(str(row["source_id"]), {})
            record["size_bytes"] = acc.get("size_bytes", 0)
        detail_buf.append(record)
        if len(detail_buf) >= DETAIL_SHARD:
            write_json(os.path.join(data_out, "det-%04d.json" % detail_no),
                       {r["uid"]: r for r in detail_buf})
            detail_no += 1
            detail_buf = []
        if len(items) % 20000 == 0:
            log("  %s건" % format(len(items), ","))
    if detail_buf:
        write_json(os.path.join(data_out, "det-%04d.json" % detail_no), {r["uid"]: r for r in detail_buf})
        detail_no += 1

    log("총 %s건 · 상세 샤드 %d개" % (format(len(items), ","), detail_no))

    # uid -> 상세 샤드 번호
    shard_of = {}
    for i, item in enumerate(items):
        shard_of[item["uid"]] = i // DETAIL_SHARD

    # 기본 정렬(수정일 내림차순)으로 페이지 파일 생성 - 첫 화면을 빠르게
    order = sorted(range(len(items)), key=lambda i: (items[i]["m"] or "", items[i]["uid"]), reverse=True)
    pages = 0
    for start in range(0, min(len(order), FIRST_PAGES * PAGE_SIZE), PAGE_SIZE):
        chunk = [items[i] for i in order[start:start + PAGE_SIZE]]
        write_json(os.path.join(data_out, "page-%04d.json" % pages), chunk)
        pages += 1
    log("첫 화면용 목록 페이지 %d개(그 뒤는 검색 색인으로 처리)" % pages)

    # 검색 색인 샤드
    per = (len(items) + INDEX_SHARDS - 1) // INDEX_SHARDS
    for n in range(INDEX_SHARDS):
        chunk = items[n * per:(n + 1) * per]
        if not chunk:
            continue
        write_json(os.path.join(data_out, "idx-%02d.json" % n), chunk)
    log("검색 색인 샤드 %d개" % INDEX_SHARDS)

    # 수집 시각
    snap_meta = {}
    manifest_path = os.path.join(SNAP_DIR, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
        snap_meta = manifest.get("crawl_meta") or {}

    def stamp(key):
        return str(snap_meta.get(key) or "").strip().strip('"')[:19]

    collected = {
        "aihub": stamp("aihub_crawled_at"),
        "public_list": stamp("public_list_crawled_at"),
        "public_preview": stamp("public_preview_last_run"),
        "last_item": max(stamps) if stamps else "",
    }
    collected["latest"] = max([v for v in collected.values() if v] or [""])

    meta = {
        "total": len(items),
        "sources": [{"source": s, "count": c,
                     "previews": sum(v for k, v in preview_count.items() if k in ("ok", "none", "not_applicable"))
                     if s == "공공데이터포털" else c}
                    for s, c in sorted(source_count.items())],
        "aihub_access": {"다운로드": sum(1 for i in items if i["s"] == 1 and i["a"]["type"] == "다운로드"),
                         "안심존": sum(1 for i in items if i["s"] == 1 and i["a"]["type"] == "안심존")},
        "fields": [{"value": k, "count": v} for k, v in field_count.most_common(60)],
        "formats": [{"value": k, "count": v} for k, v in format_count.most_common(40)],
        "access": dict(access_count),
        "collected": collected,
        "meta": snap_meta,
        "shards": {"pages": pages, "page_size": PAGE_SIZE, "detail": detail_no,
                   "detail_size": DETAIL_SHARD, "index": INDEX_SHARDS},
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "database": True,
    }
    write_json(os.path.join(data_out, "meta.json"), meta)
    write_json(os.path.join(data_out, "shard-map.json"), shard_of)

    total_mb = sum(os.path.getsize(os.path.join(data_out, f)) for f in os.listdir(data_out)) / 1048576
    log("완료: %.0fMB, 파일 %d개, %.0f초"
        % (total_mb, len(os.listdir(data_out)) + 2, time.time() - started))
    return meta


def copy_shell(out_dir):
    """대시보드 HTML 과 정적 데이터 계층을 산출물로 복사한다."""
    shutil.copyfile(os.path.join(ROOT, "catalog.html"), os.path.join(out_dir, "index.html"))
    for name in ("static-api.js",):
        src = os.path.join(ROOT, "web", name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(out_dir, name))
    headers_src = os.path.join(ROOT, "web", "_headers")
    if os.path.exists(headers_src):
        shutil.copyfile(headers_src, os.path.join(out_dir, "_headers"))
    log("index.html / static-api.js 복사 완료")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    args = ap.parse_args()
    out_dir = os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)
    build(out_dir)
    copy_shell(out_dir)


if __name__ == "__main__":
    main()
