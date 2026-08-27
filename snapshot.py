# -*- coding: utf-8 -*-
"""카탈로그 스냅샷 - 깃에 올릴 수 있는 크기로 내보내고 다시 DB로 복원한다.

catalog.db 는 원본 HTML·미리보기 본문까지 담고 있어 수백 MB가 되므로 깃에 올리지 않는다.
대신 목록·상세 핵심 필드만 골라 gzip JSONL 로 나눠 저장하고(파트당 40MB 이하),
clone 한 쪽에서는 restore 로 catalog.db 를 다시 만든다.

  python snapshot.py export     # data/catalog.db      -> data/snapshot/*.jsonl.gz
  python snapshot.py restore    # data/snapshot/*.jsonl.gz -> data/catalog.db
  python snapshot.py info       # 스냅샷/DB 상태 확인
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time

from catalog_db import DATA_DIR, DB_PATH, connect, decode_json, init_db, set_meta

SNAP_DIR = os.path.join(DATA_DIR, "snapshot")
MANIFEST = os.path.join(SNAP_DIR, "manifest.json")
PART_LIMIT = 38 * 1024 * 1024  # 파트당 압축 후 목표 상한(깃허브 권고 50MB 이하)

# 스냅샷에 담는 컬럼(원본 HTML/미리보기 본문 제외)
COLUMNS = [
    "uid", "source", "source_id", "list_order", "title", "file_name", "field", "subfield",
    "organization", "organization_code", "organization_type", "formats_json", "keywords_json",
    "description", "modified_at", "created_at", "next_update", "update_cycle", "media_type",
    "row_count", "views", "downloads", "url", "detail_pk", "detail_status", "preview_status",
    "error", "crawled_at",
]
# 공공데이터포털 상세에서 유지할 키(설명·키워드 등 목록에 이미 있는 값은 제외)
DETAIL_KEEP = {
    "record_type", "classification", "department", "contact", "basis", "collection_method",
    "update_cycle", "next_update", "media_type", "row_count", "extension", "limitations",
    "delivery", "notes", "spatial", "temporal", "paid", "charge_basis", "license",
    "national_core", "standardized", "api_type", "traffic", "review_type",
}
DESC_LIMIT = 1200  # 설명은 앞부분만 유지


def trim_detail(source: str, detail: dict) -> dict:
    if not detail:
        return {}
    if source == "AI Hub":
        # AI Hub 상세 본문은 data/details/<sn>.json 으로 따로 관리하므로 DB 쪽은 비운다.
        return {}
    out = {k: v for k, v in detail.items() if k in DETAIL_KEEP and v not in ("", None, [], {})}
    cols = detail.get("columns")
    if isinstance(cols, dict) and cols.get("headers"):
        out["columns"] = {"headers": cols["headers"][:200]}
    return out


def trim_preview(preview: dict) -> dict:
    """미리보기는 컬럼명만 남긴다(본문 행은 재수집 가능하고 용량이 매우 크다)."""
    if not preview or not preview.get("headers"):
        return {}
    out = {"headers": preview["headers"][:200], "rows": []}
    if preview.get("note"):
        out["note"] = str(preview["note"])[:300]
    if preview.get("source"):
        out["source"] = preview["source"]
    out["row_sample_dropped"] = len(preview.get("rows") or [])
    return out


def export() -> None:
    if not os.path.exists(DB_PATH):
        print("catalog.db 가 없습니다. 먼저 수집기를 실행하세요.")
        return
    os.makedirs(SNAP_DIR, exist_ok=True)
    for name in os.listdir(SNAP_DIR):
        if name.endswith(".jsonl.gz"):
            os.remove(os.path.join(SNAP_DIR, name))

    con = connect(DB_PATH, readonly=True)
    total = con.execute("SELECT COUNT(*) FROM catalog_items WHERE active=1").fetchone()[0]
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM crawl_meta")}

    parts, part_no, rows_in_part, written = [], 1, 0, 0
    path = os.path.join(SNAP_DIR, "items-%02d.jsonl.gz" % part_no)
    fh = gzip.open(path, "wt", encoding="utf-8", compresslevel=9)
    select = "SELECT " + ",".join(COLUMNS) + ",detail_json,preview_json FROM catalog_items WHERE active=1 ORDER BY source,list_order,uid"
    try:
        for row in con.execute(select):
            item = {c: row[c] for c in COLUMNS}
            if item.get("description"):
                item["description"] = item["description"][:DESC_LIMIT]
            item["detail"] = trim_detail(row["source"], decode_json(row["detail_json"], {}))
            item["preview"] = trim_preview(decode_json(row["preview_json"], {}))
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            written += 1
            rows_in_part += 1
            if rows_in_part % 2000 == 0:
                fh.flush()
                if os.path.getsize(path) >= PART_LIMIT:
                    fh.close()
                    parts.append(os.path.basename(path))
                    print("  파트 %s 완료 (%s행, %.1fMB)" % (os.path.basename(path), rows_in_part, os.path.getsize(path) / 1048576))
                    part_no += 1
                    rows_in_part = 0
                    path = os.path.join(SNAP_DIR, "items-%02d.jsonl.gz" % part_no)
                    fh = gzip.open(path, "wt", encoding="utf-8", compresslevel=9)
            if written % 20000 == 0:
                print("  %s / %s" % (written, total), flush=True)
    finally:
        fh.close()
        con.close()
    if rows_in_part:
        parts.append(os.path.basename(path))
        print("  파트 %s 완료 (%s행, %.1fMB)" % (os.path.basename(path), rows_in_part, os.path.getsize(path) / 1048576))
    elif os.path.exists(path):
        os.remove(path)

    manifest = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": written,
        "parts": parts,
        "crawl_meta": meta,
        "note": "미리보기 본문 행과 원본 HTML은 용량 때문에 제외되어 있습니다. python crawl_data_go_kr.py --missing-previews 로 다시 채울 수 있습니다.",
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    size = sum(os.path.getsize(os.path.join(SNAP_DIR, p)) for p in parts)
    print("스냅샷 완료: %s행, %s파트, 총 %.1fMB -> %s" % (written, len(parts), size / 1048576, SNAP_DIR))


def restore(force: bool = False) -> int:
    if not os.path.exists(MANIFEST):
        print("스냅샷이 없습니다: %s" % MANIFEST)
        return 0
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    con = connect(DB_PATH)
    init_db(con)
    have = con.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0]
    if have and not force:
        print("catalog.db 에 이미 %s건이 있어 복원을 건너뜁니다. (다시 만들려면 --force)" % have)
        con.close()
        return 0

    insert = (
        "INSERT INTO catalog_items (" + ",".join(COLUMNS) + ",active,detail_json,preview_json) "
        "VALUES (" + ",".join("?" * len(COLUMNS)) + ",1,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET " +
        ",".join("%s=excluded.%s" % (c, c) for c in COLUMNS if c != "uid") +
        ",active=1,detail_json=excluded.detail_json,preview_json=excluded.preview_json"
    )
    n = 0
    con.execute("BEGIN")
    for part in manifest.get("parts", []):
        path = os.path.join(SNAP_DIR, part)
        if not os.path.exists(path):
            print("  파트 없음: %s" % part)
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                values = [item.get(c) if item.get(c) is not None else ("" if c not in
                          ("list_order", "row_count", "views", "downloads") else 0) for c in COLUMNS]
                values.append(json.dumps(item.get("detail") or {}, ensure_ascii=False))
                values.append(json.dumps(item.get("preview") or {}, ensure_ascii=False))
                con.execute(insert, values)
                uid = item["uid"]
                con.execute("DELETE FROM item_formats WHERE uid=?", (uid,))
                for value in decode_json(item.get("formats_json"), []) or []:
                    con.execute("INSERT OR IGNORE INTO item_formats(uid,format) VALUES(?,?)", (uid, str(value).upper()))
                n += 1
                if n % 20000 == 0:
                    print("  %s건 복원" % n, flush=True)
    for key, value in (manifest.get("crawl_meta") or {}).items():
        set_meta(con, key, value)
    set_meta(con, "restored_from_snapshot", manifest.get("exported_at", ""))
    con.commit()
    con.close()
    print("복원 완료: %s건" % n)
    return n


def info() -> None:
    if os.path.exists(DB_PATH):
        con = connect(DB_PATH, readonly=True)
        rows = [dict(r) for r in con.execute("SELECT source,COUNT(*) c FROM catalog_items WHERE active=1 GROUP BY source")]
        con.close()
        print("catalog.db  %.1fMB  %s" % (os.path.getsize(DB_PATH) / 1048576, rows))
    else:
        print("catalog.db 없음")
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            m = json.load(f)
        size = sum(os.path.getsize(os.path.join(SNAP_DIR, p)) for p in m.get("parts", [])
                   if os.path.exists(os.path.join(SNAP_DIR, p)))
        print("스냅샷      %.1fMB  %s건  %s파트  (%s)" % (size / 1048576, m.get("count"), len(m.get("parts", [])), m.get("exported_at")))
    else:
        print("스냅샷 없음")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "export":
        export()
    elif cmd == "restore":
        restore(force="--force" in sys.argv)
    else:
        info()
