# -*- coding: utf-8 -*-
"""
공공데이터포털 파일데이터 전체 수집기.

기본 실행은 현재 목록 -> 공식 월간 메타데이터 -> 누락 상세 -> 미리보기 순서로
수집하며, SQLite 상태를 이용해 중간에 종료되어도 다음 실행에서 이어받는다.

  python crawl_data_go_kr.py                    # 전체 수집/재개
  python crawl_data_go_kr.py --list --catalog  # 목록과 상세 메타데이터만 빠르게 갱신
  python crawl_data_go_kr.py --previews         # 남은 미리보기 이어받기
  python crawl_data_go_kr.py --previews --limit 100
  python crawl_data_go_kr.py --details --id 15118669
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from catalog_db import DB_PATH, ROOT, connect, init_db, now, replace_formats, set_meta


BASE = "https://www.data.go.kr"
LIST_URL = BASE + "/tcs/dss/selectDataSetList.do"
DETAIL_URL = BASE + "/data/{pk}/fileData.do"
PREVIEW_URL = BASE + "/tcs/dss/selectHistAndCsvData.do"
MASTER_PK = "15062804"
PER_PAGE = 1000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}
RAW_ROOT = os.path.join(ROOT, "raw", "data_go_kr")
RAW_LIST = os.path.join(RAW_ROOT, "list")
RAW_CATALOG = os.path.join(RAW_ROOT, "catalog")
for directory in (RAW_LIST, RAW_CATALOG):
    os.makedirs(directory, exist_ok=True)


_local = threading.local()


def session() -> requests.Session:
    if not getattr(_local, "session", None):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    return _local.session


def fetch(url: str, *, params=None, data=None, method="get", retries=5, timeout=80) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            response = getattr(session(), method)(url, params=params, data=data, timeout=timeout)
            if response.status_code == 200 and response.content:
                return response
            last = "HTTP %s (%s bytes)" % (response.status_code, len(response.content))
        except requests.RequestException as exc:
            last = repr(exc)
        time.sleep(min(12, 1.2 * (2 ** attempt)))
    raise RuntimeError("요청 실패: %s (%s)" % (url, last))


def clean(value) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def to_int(value) -> int:
    value = re.sub(r"[^0-9]", "", str(value or ""))
    return int(value) if value else 0


def unique(values):
    result, seen = [], set()
    for value in values:
        value = clean(value)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def split_formats(values):
    result = []
    for value in values if isinstance(values, list) else [values]:
        result.extend(re.split(r"\s*[+,/]\s*", clean(value)))
    return [x.upper() for x in unique(result) if x]


def table_data(table, max_rows=250, max_cell=20000):
    if table is None:
        return {"headers": [], "rows": []}
    trs = table.find_all("tr")
    headers = [clean(x.get_text(" "))[:max_cell] for x in (trs[0].find_all(["th", "td"]) if trs else [])]
    rows = []
    for tr in trs[1:max_rows + 1]:
        cells = [clean(x.get_text(" "))[:max_cell] for x in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    return {"headers": headers, "rows": rows}


# ---------------------------------------------------------------------------
# 현재 목록

def list_params(page):
    return {
        "sType": "total", "dType": "FILE", "sort": "date",
        "currentPage": str(page), "perPage": str(PER_PAGE),
    }


def get_list_page(page: int, refresh=False) -> str:
    path = os.path.join(RAW_LIST, "page_%04d.html.gz" % page)
    if os.path.exists(path) and not refresh:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    text = fetch(LIST_URL, params=list_params(page)).text
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        f.write(text)
    os.replace(tmp, path)
    return text


def parse_list_page(html: str, page: int):
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean(soup.get_text(" "))
    match = re.search(r"파일데이터\s*\(([\d,]+)건\)", page_text)
    total = to_int(match.group(1)) if match else 0
    records = []
    for offset, item in enumerate(soup.select(".apply-result-item")):
        link = item.select_one('.apply-result-link a[href*="/data/"][href$="/fileData.do"]')
        if link is None:
            continue
        match = re.search(r"/data/(\d+)/fileData\.do", link.get("href", ""))
        if not match:
            continue
        pk = match.group(1)
        badges = [clean(x.get_text(" ")) for x in item.select(".apply-result-category .krds-badge")]
        raw_formats = [clean(x.get_text(" ")) for x in item.select(".apply-result-link .krds-badge")]
        info = {}
        for li in item.select(".in-result-item > ul > li"):
            key = li.find("strong")
            if key:
                key_text = clean(key.get_text(" "))
                key.extract()
                info[key_text] = clean(li.get_text(" "))
        detail_pk = ""
        for button in item.select("button[onclick]"):
            onclick = button.get("onclick", "")
            found = re.search(r"fn_fileDataDown\(\s*'%s'\s*,\s*'([^']+)'" % re.escape(pk), onclick)
            if found:
                detail_pk = found.group(1)
                break
        records.append({
            "uid": "public:" + pk,
            "source": "공공데이터포털",
            "source_id": pk,
            "active": 1,
            "list_order": (page - 1) * PER_PAGE + offset,
            "title": clean(link.get_text(" ")),
            "file_name": clean(link.get_text(" ")),
            "field": badges[0] if badges else "",
            "subfield": "",
            "organization": info.get("제공기관", ""),
            "organization_code": "",
            "organization_type": badges[1] if len(badges) > 1 else "",
            "formats": split_formats(raw_formats),
            "keywords": unique(info.get("키워드", "").split(",")),
            "description": clean(item.select_one(".apply-result-summary").get_text(" "))
                           if item.select_one(".apply-result-summary") else "",
            "modified_at": info.get("수정일", ""),
            "views": to_int(info.get("조회수")),
            "downloads": to_int(info.get("다운로드")),
            "url": urljoin(BASE, link.get("href", "")),
            "detail_pk": detail_pk,
        })
    return records, total


LIST_UPSERT = """
INSERT INTO catalog_items (
    uid,source,source_id,active,list_order,title,file_name,field,subfield,organization,
    organization_code,organization_type,formats_json,keywords_json,description,modified_at,
    views,downloads,url,detail_pk,crawled_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(uid) DO UPDATE SET
    active=1,list_order=excluded.list_order,title=excluded.title,file_name=excluded.file_name,
    field=excluded.field,organization=excluded.organization,organization_type=excluded.organization_type,
    formats_json=excluded.formats_json,keywords_json=excluded.keywords_json,
    description=excluded.description,modified_at=excluded.modified_at,views=excluded.views,
    downloads=excluded.downloads,url=excluded.url,
    detail_pk=CASE WHEN excluded.detail_pk<>'' THEN excluded.detail_pk ELSE catalog_items.detail_pk END,
    crawled_at=excluded.crawled_at,error=''
"""


def crawl_list(con, workers=8, refresh=False):
    first_html = get_list_page(1, refresh=refresh)
    first, total = parse_list_page(first_html, 1)
    if not total:
        raise RuntimeError("목록 총건수를 찾지 못했습니다")
    pages = (total + PER_PAGE - 1) // PER_PAGE
    all_records = {x["source_id"]: x for x in first}
    print(f"[목록] 사이트 총 {total:,}건 / {pages:,}페이지", flush=True)

    def work(page):
        return page, parse_list_page(get_list_page(page, refresh=refresh), page)[0]

    if pages > 1:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(work, page): page for page in range(2, pages + 1)}
            done = 1
            for future in as_completed(futures):
                page, records = future.result()
                for record in records:
                    all_records[record["source_id"]] = record
                done += 1
                print(f"[목록] {done}/{pages}페이지 · 현재 {len(all_records):,}건", flush=True)

    # 최신순 목록은 수집 도중 새 항목이 들어오면 페이지 경계가 움직일 수 있다.
    # 총건수보다 적을 때 한 번 더 훑어 첫 패스에서 경계 밖으로 밀린 항목을 합친다.
    repair = 0
    while len(all_records) < total and repair < 2:
        repair += 1
        before = len(all_records)
        print(f"[목록 보정] {total - before:,}건 부족 · {repair}차 재확인", flush=True)
        def repair_work(page):
            return page, parse_list_page(get_list_page(page, refresh=True), page)[0]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
            futures = {pool.submit(repair_work, page): page for page in range(1, pages + 1)}
            checked = 0
            for future in as_completed(futures):
                _page, records = future.result()
                for record in records:
                    all_records[record["source_id"]] = record
                checked += 1
                if checked % 12 == 0 or checked == pages:
                    print(f"[목록 보정] {checked}/{pages}페이지 · {len(all_records):,}건", flush=True)
        if len(all_records) == before:
            break

    stamp = now()
    con.execute("BEGIN")
    con.execute("UPDATE catalog_items SET active=0 WHERE source='공공데이터포털'")
    for record in all_records.values():
        values = (
            record["uid"], record["source"], record["source_id"], 1, record["list_order"],
            record["title"], record["file_name"], record["field"], record["subfield"],
            record["organization"], record["organization_code"], record["organization_type"],
            json.dumps(record["formats"], ensure_ascii=False),
            json.dumps(record["keywords"], ensure_ascii=False), record["description"],
            record["modified_at"], record["views"], record["downloads"], record["url"],
            record["detail_pk"], stamp,
        )
        con.execute(LIST_UPSERT, values)
        replace_formats(con, record["uid"], record["formats"])
    set_meta(con, "public_total_site", total)
    set_meta(con, "public_list_count", len(all_records))
    set_meta(con, "public_list_crawled_at", stamp)
    con.commit()
    print(f"[목록 완료] {len(all_records):,}건", flush=True)
    return len(all_records)


# ---------------------------------------------------------------------------
# 공공데이터포털이 직접 제공하는 월간 전체 메타데이터 CSV

def discover_master_download():
    html = fetch(DETAIL_URL.format(pk=MASTER_PK)).text
    soup = BeautifulSoup(html, "html.parser")
    button = next((b for b in soup.select("button[onclick]") if "fn_fileDataDown" in b.get("onclick", "")), None)
    if button is None:
        raise RuntimeError("목록개방현황 다운로드 버튼을 찾지 못했습니다")
    args = re.findall(r"'([^']*)'", button.get("onclick", ""))
    if len(args) < 5:
        raise RuntimeError("목록개방현황 다운로드 인수를 읽지 못했습니다")
    pk, detail_pk, atch_file_id, file_sn, hist_sn = args[:5]
    params = {
        "publicDataDetailPk": detail_pk, "publicDataPk": pk,
        "atchFileId": atch_file_id, "fileDetailSn": file_sn,
        "publicDataTyCode": "PR0051", "publicDataHistSn": hist_sn,
    }
    response = fetch(BASE + "/tcs/dss/selectFileDataDownload.do", params=params)
    info = response.json()
    if not info.get("status"):
        raise RuntimeError("목록개방현황 다운로드 정보 오류: %s" % info)
    data = info.get("dataSetFileDetailInfo") or {}
    return {
        "atch_file_id": info["atchFileId"],
        "file_sn": info["fileDetailSn"],
        "data_name": data.get("dataNm") or data.get("publicDataSj") or "public_data_catalog",
        "detail_pk": detail_pk,
    }


def download_master(refresh=False):
    info = discover_master_download()
    params = {
        "atchFileId": info["atch_file_id"], "fileDetailSn": info["file_sn"],
        "dataNm": info["data_name"],
    }
    response = session().get(BASE + "/cmm/cmm/fileDownload.do", params=params, stream=True, timeout=120)
    response.raise_for_status()
    # 일부 응답의 Content-Disposition 한글 파일명이 ISO-8859-1로 깨져 전달된다.
    # 날짜만 보존한 ASCII 파일명으로 고정해 Windows 콘솔/파일시스템 문제를 피한다.
    date_match = re.search(r"20\d{6}", info["data_name"])
    filename = "public_data_catalog_%s.csv" % (date_match.group(0) if date_match else datetime.now().strftime("%Y%m%d"))
    path = os.path.join(RAW_CATALOG, filename)
    expected = to_int(response.headers.get("Content-Length"))
    if os.path.exists(path) and not refresh and (not expected or os.path.getsize(path) == expected):
        response.close()
        print("[공식 메타데이터] 캐시 사용: %s" % os.path.basename(path), flush=True)
        return path
    tmp = path + ".tmp"
    got = 0
    with open(tmp, "wb") as f:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)
                got += len(chunk)
                if got % (20 * 1024 * 1024) < len(chunk):
                    print("[공식 메타데이터] 다운로드 %.0f MB" % (got / 1024 / 1024), flush=True)
    os.replace(tmp, path)
    print("[공식 메타데이터] 저장: %s (%.1f MB)" % (filename, got / 1024 / 1024), flush=True)
    return path


MASTER_KEYS = {
    "목록키": "source_id", "목록유형": "record_type", "목록명": "title",
    "파일데이터명": "file_name", "분류체계": "classification", "제공기관코드": "organization_code",
    "제공기관": "organization", "관리 부서명": "department", "관리부서 전화번호": "contact",
    "보유근거": "basis", "수집방법": "collection_method", "업데이트 주기": "update_cycle",
    "차기 등록 예정일": "next_update", "매체유형": "media_type", "전체행": "row_count",
    "확장자(데이터포맷)": "extension", "키워드": "keywords", "다운로드_활용신청건수": "downloads",
    "등록일": "created_at", "수정일": "modified_at", "데이터 한계": "limitations",
    "제공형태": "delivery", "설명": "description", "기타 유의사항": "notes",
    "공간범위": "spatial", "시간범위": "temporal", "비용부과유무": "paid",
    "비용부과기준 및 단위": "charge_basis", "이용허락범위": "license",
    "API 유형": "api_type", "신청가능 트래픽": "traffic", "심의 유형": "review_type",
    "조회수": "views", "목록 URL": "url", "국가중점여부": "national_core",
    "표준데이터여부": "standardized",
}


def import_master(con, path):
    active_ids = {row[0] for row in con.execute(
        "SELECT source_id FROM catalog_items WHERE source='공공데이터포털' AND active=1"
    )}
    if not active_ids:
        raise RuntimeError("현재 목록이 없습니다. --list를 먼저 실행하세요.")
    updated = 0
    stamp = now()
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if clean(row.get("목록유형")) != "FILE":
                continue
            pk = clean(row.get("목록키"))
            if pk not in active_ids:
                continue
            detail = {eng: clean(row.get(kor)) for kor, eng in MASTER_KEYS.items()}
            detail["row_count"] = to_int(detail.get("row_count"))
            detail["views"] = to_int(detail.get("views"))
            detail["downloads"] = to_int(detail.get("downloads"))
            detail["keywords"] = unique(detail.get("keywords", "").split(","))
            classification = detail.get("classification", "")
            subfield = clean(classification.split(" - ", 1)[1]) if " - " in classification else ""
            formats = split_formats(detail.get("extension", ""))
            uid = "public:" + pk
            old_formats = []
            found = con.execute("SELECT formats_json FROM catalog_items WHERE uid=?", (uid,)).fetchone()
            if found:
                try:
                    old_formats = json.loads(found[0])
                except (TypeError, ValueError):
                    pass
            formats = unique([x.upper() for x in old_formats + formats])
            con.execute(
                """UPDATE catalog_items SET
                   file_name=?,subfield=?,organization_code=?,organization=CASE WHEN organization='' THEN ? ELSE organization END,
                   formats_json=?,keywords_json=?,description=CASE WHEN ?<>'' THEN ? ELSE description END,
                   modified_at=CASE WHEN modified_at='' THEN ? ELSE modified_at END,created_at=?,next_update=?,
                   update_cycle=?,media_type=?,row_count=?,url=CASE WHEN url='' THEN ? ELSE url END,
                   detail_status='bulk',detail_json=?,crawled_at=? WHERE uid=?""",
                (detail.get("file_name", ""), subfield, detail.get("organization_code", ""),
                 detail.get("organization", ""), json.dumps(formats, ensure_ascii=False),
                 json.dumps(detail["keywords"], ensure_ascii=False), detail.get("description", ""),
                 detail.get("description", ""), detail.get("modified_at", ""), detail.get("created_at", ""),
                 detail.get("next_update", ""), detail.get("update_cycle", ""), detail.get("media_type", ""),
                 detail["row_count"], detail.get("url", ""), json.dumps(detail, ensure_ascii=False), stamp, uid),
            )
            replace_formats(con, uid, formats)
            updated += 1
            if updated % 5000 == 0:
                con.commit()
                print(f"[상세 메타데이터] {updated:,}건", flush=True)
    set_meta(con, "public_catalog_path", os.path.relpath(path, ROOT))
    set_meta(con, "public_catalog_imported", updated)
    set_meta(con, "public_catalog_imported_at", stamp)
    con.commit()
    print(f"[상세 메타데이터 완료] {updated:,}건", flush=True)
    return updated


# ---------------------------------------------------------------------------
# 월간 CSV에 아직 없는 신규 데이터의 상세 페이지

def parse_detail_page(pk, html):
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    bodies = soup.select(".data-info-body")
    body = next((b for b in bodies if b.select_one(".key")), None)
    if body:
        for li in body.select("li"):
            key = li.select_one(".key")
            value = li.select_one(".value")
            if key and value:
                info[clean(key.get_text(" "))] = clean(value.get_text(" "))
    hidden = soup.select_one('input[name="publicDataDetailPk"]')
    columns = table_data(soup.select_one(".data-column-box table"))
    title = soup.select_one("h1.h-tit")
    return {
        "pk": pk,
        "title": clean(title.get_text(" ")) if title else "",
        "detail_pk": hidden.get("value", "") if hidden else "",
        "info": info,
        "columns": columns,
    }


def fetch_detail_record(pk):
    parsed = parse_detail_page(pk, fetch(DETAIL_URL.format(pk=pk)).text)
    if not parsed["info"]:
        raise RuntimeError("상세 정보가 비어 있습니다")
    return parsed


def crawl_details(con, workers=10, limit=0, only_id="", force=False):
    where = ["source='공공데이터포털'", "active=1"]
    args = []
    if only_id:
        where.append("source_id=?")
        args.append(str(only_id))
    elif not force:
        where.append("detail_status NOT IN ('bulk','ok')")
    sql = "SELECT source_id,detail_json FROM catalog_items WHERE " + " AND ".join(where) + " ORDER BY list_order"
    if limit:
        sql += " LIMIT %d" % int(limit)
    targets = list(con.execute(sql, args))
    if not targets:
        print("[신규 상세] 수집 대상 없음", flush=True)
        return 0
    print(f"[신규 상세] {len(targets):,}건", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_detail_record, row["source_id"]): row for row in targets}
        for future in as_completed(futures):
            row = futures[future]
            pk = row["source_id"]
            try:
                result = future.result()
                detail = {}
                try:
                    detail = json.loads(row["detail_json"] or "{}")
                except ValueError:
                    pass
                detail.update(result["info"])
                detail["columns"] = result["columns"]
                con.execute(
                    "UPDATE catalog_items SET detail_pk=CASE WHEN ?<>'' THEN ? ELSE detail_pk END,"
                    "detail_status='ok',detail_json=?,error='',crawled_at=? WHERE source_id=? AND source='공공데이터포털'",
                    (result["detail_pk"], result["detail_pk"], json.dumps(detail, ensure_ascii=False), now(), pk),
                )
            except Exception as exc:
                con.execute(
                    "UPDATE catalog_items SET detail_status='error',error=? WHERE source_id=? AND source='공공데이터포털'",
                    (str(exc)[:1000], pk),
                )
            done += 1
            if done % 50 == 0 or done == len(targets):
                con.commit()
                print(f"[신규 상세] {done:,}/{len(targets):,}", flush=True)
    con.commit()
    return done


# ---------------------------------------------------------------------------
# 포털이 화면에 표시하는 데이터 미리보기

def fetch_preview_record(pk, detail_pk):
    response = fetch(
        PREVIEW_URL,
        params={"publicDataPk": pk, "publicDataDetailPk": detail_pk},
        timeout=100,
    )
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.select_one(".preview-box table") or soup.find("table")
    preview = table_data(table)
    note = soup.select_one(".data-info-tit p")
    preview["note"] = clean(note.get_text(" ")) if note else ""
    preview["source"] = PREVIEW_URL
    if table is None:
        message = clean(soup.get_text(" "))[:2000]
        preview["message"] = message
        status = "none"
    else:
        status = "ok"
    return status, preview


def crawl_previews(con, workers=12, limit=0, only_id="", force=False):
    where = ["source='공공데이터포털'", "active=1", "detail_pk<>''"]
    args = []
    if only_id:
        where.append("source_id=?")
        args.append(str(only_id))
    elif not force:
        where.append("preview_status NOT IN ('ok','none')")
    sql = "SELECT source_id,detail_pk FROM catalog_items WHERE " + " AND ".join(where) + " ORDER BY list_order"
    if limit:
        sql += " LIMIT %d" % int(limit)
    targets = list(con.execute(sql, args))
    if not targets:
        print("[미리보기] 수집 대상 없음", flush=True)
        return 0
    print(f"[미리보기] {len(targets):,}건 · 중단 후 재실행 가능", flush=True)
    completed = errors = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(fetch_preview_record, row["source_id"], row["detail_pk"]): row["source_id"]
            for row in targets
        }
        for future in as_completed(futures):
            pk = futures[future]
            try:
                status, preview = future.result()
                con.execute(
                    "UPDATE catalog_items SET preview_status=?,preview_json=?,error='',crawled_at=? "
                    "WHERE source_id=? AND source='공공데이터포털'",
                    (status, json.dumps(preview, ensure_ascii=False), now(), pk),
                )
            except Exception as exc:
                errors += 1
                con.execute(
                    "UPDATE catalog_items SET preview_status='error',error=? "
                    "WHERE source_id=? AND source='공공데이터포털'",
                    (str(exc)[:1000], pk),
                )
            completed += 1
            if completed % 50 == 0 or completed == len(targets):
                con.commit()
                elapsed = max(1, time.time() - started)
                rate = completed / elapsed
                remain = int((len(targets) - completed) / rate) if rate else 0
                set_meta(con, "public_preview_progress", {
                    "done": completed, "target": len(targets), "errors": errors,
                    "remaining_seconds": remain, "updated_at": now(),
                })
                con.commit()
                print(f"[미리보기] {completed:,}/{len(targets):,} · 오류 {errors} · "
                      f"약 {remain // 60}분 남음", flush=True)
    set_meta(con, "public_preview_last_run", now())
    con.commit()
    return completed


def fetch_missing_preview_record(pk):
    """목록 카드에 detailPk가 없는 항목은 상세 페이지에서 키와 미리보기 유무를 확인한다."""
    html = fetch(DETAIL_URL.format(pk=pk), timeout=100).text
    parsed = parse_detail_page(pk, html)
    soup = BeautifulSoup(html, "html.parser")
    has_preview = bool(soup.select_one('a[href="#tab-layer-file-04"]'))
    if has_preview and parsed["detail_pk"]:
        status_value, preview = fetch_preview_record(pk, parsed["detail_pk"])
    else:
        status_value, preview = "not_applicable", {
            "headers": [], "rows": [],
            "message": "공공데이터포털 상세 페이지에서 미리보기를 제공하지 않는 파일입니다.",
        }
    return parsed, status_value, preview


def crawl_missing_previews(con, workers=12, limit=0, only_id="", force=False):
    """PDF·외부링크·일부 CSV처럼 목록에 detailPk가 없는 항목을 완결한다."""
    where = ["source='공공데이터포털'", "active=1", "detail_pk=''",
             "preview_status NOT IN ('ok','none','not_applicable')"]
    args = []
    if only_id:
        where.append("source_id=?")
        args.append(str(only_id))
    sql = "SELECT source_id,detail_json FROM catalog_items WHERE " + " AND ".join(where) + " ORDER BY list_order"
    if limit:
        sql += " LIMIT %d" % int(limit)
    targets = list(con.execute(sql, args))
    if not targets:
        print("[미리보기 유무 확인] 대상 없음", flush=True)
        return 0
    print(f"[미리보기 유무 확인] {len(targets):,}건", flush=True)
    completed = errors = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_missing_preview_record, row["source_id"]): row for row in targets}
        for future in as_completed(futures):
            row = futures[future]
            pk = row["source_id"]
            try:
                parsed, status_value, preview = future.result()
                detail = {}
                try:
                    detail = json.loads(row["detail_json"] or "{}")
                except ValueError:
                    pass
                # 월간 메타데이터는 유지하고, 상세 페이지에서만 얻을 수 있는 컬럼만 보강한다.
                if parsed["columns"]["headers"]:
                    detail["columns"] = parsed["columns"]
                con.execute(
                    "UPDATE catalog_items SET detail_pk=?,preview_status=?,preview_json=?,detail_json=?,"
                    "error='',crawled_at=? WHERE source_id=? AND source='공공데이터포털'",
                    (parsed["detail_pk"], status_value, json.dumps(preview, ensure_ascii=False),
                     json.dumps(detail, ensure_ascii=False), now(), pk),
                )
            except Exception as exc:
                errors += 1
                con.execute(
                    "UPDATE catalog_items SET preview_status='error',error=? "
                    "WHERE source_id=? AND source='공공데이터포털'",
                    (str(exc)[:1000], pk),
                )
            completed += 1
            if completed % 50 == 0 or completed == len(targets):
                con.commit()
                elapsed = max(1, time.time() - started)
                rate = completed / elapsed
                remain = int((len(targets) - completed) / rate) if rate else 0
                print(f"[미리보기 유무 확인] {completed:,}/{len(targets):,} · 오류 {errors} · "
                      f"약 {remain // 60}분 남음", flush=True)
    con.commit()
    return completed


def status(con):
    rows = con.execute(
        "SELECT source,COUNT(*) count,SUM(active) active," 
        "SUM(CASE WHEN detail_status IN ('bulk','ok') THEN 1 ELSE 0 END) details," 
        "SUM(CASE WHEN preview_status IN ('ok','none','not_applicable') THEN 1 ELSE 0 END) previews "
        "FROM catalog_items GROUP BY source"
    ).fetchall()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="공공데이터포털 파일데이터 전체 수집기")
    parser.add_argument("--list", action="store_true", help="현재 파일데이터 목록 수집")
    parser.add_argument("--catalog", action="store_true", help="공식 월간 전체 상세 메타데이터 반영")
    parser.add_argument("--details", action="store_true", help="월간 자료에 없는 신규 상세 페이지 수집")
    parser.add_argument("--missing-previews", action="store_true", help="목록에 상세키가 없는 파일의 미리보기 유무 확인")
    parser.add_argument("--previews", action="store_true", help="데이터 미리보기 전체 수집/재개")
    parser.add_argument("--status", action="store_true", help="현재 수집 현황 출력")
    parser.add_argument("--refresh-list", action="store_true", help="목록 HTML 캐시 무시")
    parser.add_argument("--refresh-catalog", action="store_true", help="공식 월간 CSV 다시 다운로드")
    parser.add_argument("--force", action="store_true", help="완료된 상세/미리보기도 다시 수집")
    parser.add_argument("--workers", type=int, default=12, help="동시 요청 수(기본 12)")
    parser.add_argument("--limit", type=int, default=0, help="상세/미리보기 이번 실행 제한")
    parser.add_argument("--id", default="", help="특정 목록키만 상세/미리보기 수집")
    parser.add_argument("--db", default=DB_PATH, help="SQLite 경로")
    args = parser.parse_args()
    selected = any((args.list, args.catalog, args.details, args.missing_previews, args.previews, args.status))
    if not selected:
        args.list = args.catalog = args.details = args.missing_previews = args.previews = True

    con = connect(args.db)
    init_db(con)
    from catalog_db import import_aihub
    imported_ai = import_aihub(con)
    print(f"[AI Hub 동기화] {imported_ai:,}건", flush=True)
    try:
        if args.list:
            crawl_list(con, workers=min(args.workers, 12), refresh=args.refresh_list)
        if args.catalog:
            master = download_master(refresh=args.refresh_catalog)
            import_master(con, master)
        if args.details:
            crawl_details(con, workers=args.workers, limit=args.limit, only_id=args.id, force=args.force)
        if args.missing_previews:
            crawl_missing_previews(con, workers=args.workers, limit=args.limit, only_id=args.id, force=args.force)
        if args.previews:
            crawl_previews(con, workers=args.workers, limit=args.limit, only_id=args.id, force=args.force)
        status(con)
    finally:
        con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다. 같은 명령을 다시 실행하면 이어집니다.", file=sys.stderr)
        raise SystemExit(130)
