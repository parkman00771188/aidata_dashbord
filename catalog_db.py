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

