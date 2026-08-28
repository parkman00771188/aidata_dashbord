# -*- coding: utf-8 -*-
"""AI Hub와 공공데이터포털을 함께 조회하기 위한 로컬 SQLite 카탈로그."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime


ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "catalog.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_items (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    list_order INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL DEFAULT '',
    subfield TEXT NOT NULL DEFAULT '',
    organization TEXT NOT NULL DEFAULT '',
    organization_code TEXT NOT NULL DEFAULT '',
    organization_type TEXT NOT NULL DEFAULT '',
    formats_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    modified_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    next_update TEXT NOT NULL DEFAULT '',
    update_cycle TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    detail_pk TEXT NOT NULL DEFAULT '',
    detail_status TEXT NOT NULL DEFAULT 'pending',
    preview_status TEXT NOT NULL DEFAULT 'pending',
    detail_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    crawled_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS item_formats (
    uid TEXT NOT NULL,
    format TEXT NOT NULL,
    PRIMARY KEY (uid, format),
    FOREIGN KEY (uid) REFERENCES catalog_items(uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 데이터 항목(컬럼) 색인. 미리보기 헤더와 AI Hub 메타데이터 구조표에서 뽑아 둔다.
CREATE TABLE IF NOT EXISTS item_columns (
    uid TEXT PRIMARY KEY,
    columns_json TEXT NOT NULL DEFAULT '[]',
    columns_text TEXT NOT NULL DEFAULT '',
    n INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (uid) REFERENCES catalog_items(uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_source_active ON catalog_items(source, active);
CREATE INDEX IF NOT EXISTS idx_items_field ON catalog_items(field);
CREATE INDEX IF NOT EXISTS idx_items_organization ON catalog_items(organization);
CREATE INDEX IF NOT EXISTS idx_items_modified ON catalog_items(modified_at);
CREATE INDEX IF NOT EXISTS idx_items_downloads ON catalog_items(downloads);
CREATE INDEX IF NOT EXISTS idx_items_views ON catalog_items(views);
CREATE INDEX IF NOT EXISTS idx_items_title ON catalog_items(title);
CREATE INDEX IF NOT EXISTS idx_formats_format ON item_formats(format);
"""


def connect(path: str = DB_PATH, readonly: bool = False) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if readonly and os.path.exists(path):
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=30)
    else:
        con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    con.execute(
        "INSERT INTO crawl_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    text = str(value).replace("+", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def replace_formats(con: sqlite3.Connection, uid: str, formats) -> None:
    values = []
    seen = set()
    for value in clean_list(formats):
        value = value.upper()
        if value not in seen:
            values.append(value)
            seen.add(value)
    con.execute("DELETE FROM item_formats WHERE uid=?", (uid,))
    con.executemany("INSERT OR IGNORE INTO item_formats(uid,format) VALUES(?,?)", ((uid, x) for x in values))


def import_aihub(con: sqlite3.Connection, path: str | None = None) -> int:
    """기존 data/datasets.json을 통합 카탈로그에 동기화한다."""
    path = path or os.path.join(DATA_DIR, "datasets.json")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("datasets", [])
    stamp = now()
    con.execute("UPDATE catalog_items SET active=0 WHERE source='AI Hub'")
    sql = """
    INSERT INTO catalog_items (
        uid,source,source_id,active,list_order,title,file_name,field,subfield,
        organization,organization_code,organization_type,formats_json,keywords_json,
        description,modified_at,created_at,next_update,update_cycle,media_type,row_count,
        views,downloads,url,detail_pk,detail_status,preview_status,detail_json,preview_json,error,crawled_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(uid) DO UPDATE SET
        active=1,list_order=excluded.list_order,title=excluded.title,field=excluded.field,
        formats_json=excluded.formats_json,keywords_json=excluded.keywords_json,
        description=excluded.description,modified_at=excluded.modified_at,views=excluded.views,
        downloads=excluded.downloads,url=excluded.url,detail_status=excluded.detail_status,
        crawled_at=excluded.crawled_at
    """
    for order, item in enumerate(items):
        sn = str(item.get("sn", ""))
        if not sn:
            continue
        uid = "aihub:" + sn
        formats = clean_list(item.get("types", []))
        detail_status = "ok" if os.path.exists(os.path.join(DATA_DIR, "details", sn + ".json")) else "pending"
        values = (
            uid, "AI Hub", sn, 1, order, item.get("title", ""), item.get("title", ""),
            item.get("field", ""), "", item.get("builder_main", ""), "", "AI 데이터 플랫폼",
            json.dumps(formats, ensure_ascii=False), json.dumps(item.get("tags", []), ensure_ascii=False),
            item.get("intro", "") or item.get("purpose", ""), item.get("update_ym", ""), "", "", "",
            item.get("data_format", ""), 0, int(item.get("views") or 0), int(item.get("downloads") or 0),
            item.get("url", ""), "", detail_status, "not_applicable", "{}", "{}", "", stamp,
        )
        con.execute(sql, values)
        replace_formats(con, uid, formats)
    set_meta(con, "aihub_count", len(items))
    set_meta(con, "aihub_crawled_at", payload.get("meta", {}).get("crawled_at", ""))
    con.commit()
    return len(items)


ATTR_HEADERS = ("속성명", "항목명", "컬럼명", "필드명", "속성", "항목")


def _aihub_columns(sn: str) -> list:
    """AI Hub 상세의 메타데이터 구조표에서 속성명 열을 뽑는다."""
    path = os.path.join(DATA_DIR, "details", "%s.json" % sn)
    if not os.path.exists(path):
        return []
    try:
        from bs4 import BeautifulSoup
        with open(path, encoding="utf-8") as f:
            sections = (json.load(f).get("sections") or {})
        html = (sections.get("annotation") or "") + (sections.get("meta") or "")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    names = []
    for table in soup.find_all("table"):
        first = table.find("tr")
        if first is None:
            continue
        head = [c.get_text(strip=True) for c in first.find_all(["th", "td"])]
        idx = next((i for i, h in enumerate(head) if h in ATTR_HEADERS), None)
        if idx is None:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) <= idx:
                continue
            value = cells[idx].get_text(" ", strip=True)
            if value and len(value) <= 40 and value not in names:
                names.append(value)
    return names[:120]


def build_column_index(con: sqlite3.Connection, rebuild: bool = False) -> int:
    """데이터 항목(컬럼) 색인을 만든다. AI 추천과 검색에서 함께 쓴다."""
    init_db(con)
    if not rebuild and con.execute("SELECT COUNT(*) FROM item_columns").fetchone()[0]:
        return 0
    con.execute("DELETE FROM item_columns")
    rows = []
    # 공공데이터포털: 미리보기 헤더 → 없으면 상세의 컬럼 목록
    query = (
        "SELECT uid,json_extract(preview_json,'$.headers') ph,"
        "json_extract(detail_json,'$.columns.headers') dh "
        "FROM catalog_items WHERE source='공공데이터포털' AND active=1"
    )
    for uid, ph, dh in con.execute(query):
        cols = decode_json(ph, None) or decode_json(dh, None) or []
        # CSV 첫 칸에 BOM이 남아 있는 경우가 많아 함께 정리한다.
        cols = [str(c).replace("﻿", "").strip() for c in cols]
        cols = [c for c in cols if c][:120]
        if cols:
            rows.append((uid, json.dumps(cols, ensure_ascii=False), " ".join(cols), len(cols)))
    # AI Hub: 메타데이터 구조표의 속성명
    for (uid, sn) in con.execute("SELECT uid,source_id FROM catalog_items WHERE source='AI Hub' AND active=1"):
        cols = _aihub_columns(sn)
        if cols:
            rows.append((uid, json.dumps(cols, ensure_ascii=False), " ".join(cols), len(cols)))
    con.executemany("INSERT OR REPLACE INTO item_columns(uid,columns_json,columns_text,n) VALUES(?,?,?,?)", rows)
    set_meta(con, "column_index_built_at", now())
    set_meta(con, "column_index_count", len(rows))
    con.commit()
    return len(rows)


def build_search_index(con: sqlite3.Connection, rebuild: bool = False) -> int:
    """검색 전용 슬림 테이블 + FTS5(trigram) 색인을 만든다.

    catalog_items 행에는 상세·미리보기 JSON이 함께 들어 있어 스캔이 무겁다.
    검색에 쓰는 텍스트만 뽑아 별도 테이블에 두고, 한국어 부분일치가 가능한
    trigram 토크나이저로 색인하면 3글자 이상 키워드는 즉시(1ms 이하) 찾을 수 있다.
    """
    init_db(con)
    exists = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name='search_fts'"
    ).fetchone()[0]
    if exists and not rebuild:
        have = con.execute("SELECT COUNT(*) FROM item_search").fetchone()[0]
        if have:
            return 0
    con.executescript(
        "DROP TABLE IF EXISTS search_fts;"
        "DROP TABLE IF EXISTS item_search;"
        "CREATE TABLE item_search (uid TEXT PRIMARY KEY, title_text TEXT NOT NULL DEFAULT '',"
        " meta_text TEXT NOT NULL DEFAULT '', desc_text TEXT NOT NULL DEFAULT '');"
    )
    con.execute(
        "INSERT INTO item_search(uid,title_text,meta_text,desc_text) "
        "SELECT i.uid, i.title||' '||i.file_name, "
        "COALESCE(i.keywords_json,'')||' '||COALESCE(i.organization,'')||' '||COALESCE(c.columns_text,''), "
        "COALESCE(i.description,'') "
        "FROM catalog_items i LEFT JOIN item_columns c ON c.uid=i.uid WHERE i.active=1"
    )
    try:
        con.execute("CREATE VIRTUAL TABLE search_fts USING fts5(uid UNINDEXED, title_text, "
                    "meta_text, desc_text, tokenize='trigram')")
        con.execute("INSERT INTO search_fts(uid,title_text,meta_text,desc_text) "
                    "SELECT uid,title_text,meta_text,desc_text FROM item_search")
    except sqlite3.OperationalError as e:  # trigram 미지원 SQLite - 스캔 방식으로 동작
        print("  FTS5 trigram 색인 생략(%s) - 검색은 스캔 방식으로 동작합니다" % e)
    n = con.execute("SELECT COUNT(*) FROM item_search").fetchone()[0]
    set_meta(con, "search_index_built_at", now())
    con.commit()
    return n


def has_fts(con: sqlite3.Connection) -> bool:
    try:
        return bool(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='search_fts'").fetchone()[0])
    except sqlite3.Error:
        return False


def decode_json(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def row_summary(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["formats"] = decode_json(d.pop("formats_json", "[]"), [])
    d["keywords"] = decode_json(d.pop("keywords_json", "[]"), [])
    for key in ("detail_json", "preview_json", "error", "active", "detail_pk"):
        d.pop(key, None)
    return d

