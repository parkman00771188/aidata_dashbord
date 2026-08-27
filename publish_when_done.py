# -*- coding: utf-8 -*-
"""수집이 끝나기를 기다렸다가 스냅샷을 만들어 깃에 올린다.

  python publish_when_done.py            # 수집 완료를 기다렸다가 자동 커밋·푸시
  python publish_when_done.py --now      # 기다리지 않고 지금 바로 내보내기 + 푸시
  python publish_when_done.py --no-push  # 커밋까지만 하고 푸시는 하지 않음
  python publish_when_done.py --interval 600 --stall-minutes 30

완료 판정
  1) 미리보기 대기(pending)가 0건이 되면 완료
  2) 수집기 프로세스가 없고 진행이 --stall-minutes 동안 멈춰 있으면 완료로 간주

주의: 스냅샷은 압축 파일이라 커밋할 때마다 저장소 기록이 약 30MB씩 늘어납니다.
     수집이 끝난 뒤 한 번만 올리는 것을 권장합니다.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "catalog.db")
DONE_STATUS = ("ok", "none", "not_applicable", "error")
DATA_PATHS = ["data/snapshot", "data/datasets.json", "data/details",
              "data/my_datasets.json", "data/recommendations.json"]


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def counts() -> dict:
    """수집 진행 상황을 읽는다(읽기 전용이라 수집기를 방해하지 않는다)."""
    uri = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        rows = dict(con.execute(
            "SELECT preview_status,COUNT(*) FROM catalog_items "
            "WHERE source='공공데이터포털' AND active=1 GROUP BY preview_status").fetchall())
    finally:
        con.close()
    total = sum(rows.values())
    done = sum(rows.get(k, 0) for k in DONE_STATUS)
    return {"total": total, "done": done, "pending": rows.get("pending", 0),
            "ok": rows.get("ok", 0), "raw": rows}


def crawler_running() -> bool:
    """crawl_data_go_kr.py 프로세스가 살아 있는지 확인(확인 불가하면 True로 본다)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return True
    return "crawl_data_go_kr.py" in (out or "")


def wait_until_done(interval: int, stall_minutes: int) -> dict:
    last_done, stall_since = None, time.time()
    while True:
        try:
            c = counts()
        except Exception as e:  # DB 잠금 등 - 다음 주기에 다시 시도
            log("진행 상황 확인 실패(%s) - %d초 후 재시도" % (e.__class__.__name__, interval))
            time.sleep(interval)
            continue

        if last_done is not None and c["done"] > last_done:
            rate = (c["done"] - last_done) / interval
            eta = (c["pending"] / rate / 3600) if rate > 0 else 0
            log("진행 %d/%d (%.1f%%) · 대기 %d건 · %.1f건/분 · 예상 %.1f시간 남음"
                % (c["done"], c["total"], c["done"] / c["total"] * 100, c["pending"], rate * 60, eta))
            stall_since = time.time()
        elif last_done is None:
            log("진행 %d/%d (%.1f%%) · 대기 %d건" % (c["done"], c["total"], c["done"] / c["total"] * 100, c["pending"]))
        last_done = c["done"]

        if c["pending"] == 0:
            log("미리보기 대기 0건 - 수집 완료")
            return c
        idle_min = (time.time() - stall_since) / 60
        if idle_min >= stall_minutes and not crawler_running():
            log("수집기가 종료되었고 %.0f분간 진행이 없어 완료로 간주합니다." % idle_min)
            return c
        time.sleep(interval)


def git(*args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s 실패: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:400]))
    return r


def publish(c: dict, push: bool) -> None:
    log("스냅샷을 만듭니다…")
    import snapshot
    snapshot.export()

    existing = [p for p in DATA_PATHS if os.path.exists(os.path.join(ROOT, p))]
    git("add", "--", *existing)
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        log("변경된 데이터가 없어 커밋을 건너뜁니다.")
        return

    message = (
        "데이터 스냅샷 갱신 - 공공데이터 미리보기 %s/%s건 수집\n\n"
        "- 미리보기 수집 완료 %s건 (ok %s / 없음 %s / 대상아님 %s / 오류 %s)\n"
        "- 카탈로그 %s건 (AI Hub + 공공데이터포털)\n"
        "- data/snapshot 재생성, catalog.db 는 용량 때문에 계속 제외\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
        % (c["done"], c["total"], c["done"], c["raw"].get("ok", 0), c["raw"].get("none", 0),
           c["raw"].get("not_applicable", 0), c["raw"].get("error", 0), c["total"] + 975)
    )
    git("commit", "-m", message)
    log("커밋 완료: %s" % git("log", "-1", "--oneline").stdout.strip())

    if not push:
        log("--no-push 이므로 푸시하지 않습니다.")
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    r = subprocess.run(["git", "push", "origin", "HEAD"], cwd=ROOT, capture_output=True, text=True, env=env)
    if r.returncode == 0:
        log("푸시 완료 → %s" % git("remote", "get-url", "origin").stdout.strip())
    else:
        log("푸시 실패: %s" % (r.stderr or r.stdout).strip()[:400])
        log("원격이 앞서 있으면 `git pull --rebase` 후 `git push` 를 실행하세요.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="진행 확인 주기(초)")
    ap.add_argument("--stall-minutes", type=int, default=20, help="이만큼 진행이 없고 수집기도 없으면 완료로 간주")
    ap.add_argument("--now", action="store_true", help="기다리지 않고 즉시 실행")
    ap.add_argument("--no-push", action="store_true", help="커밋만 하고 푸시는 생략")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        log("catalog.db 가 없습니다.")
        sys.exit(1)
    c = counts() if args.now else wait_until_done(args.interval, args.stall_minutes)
    publish(c, push=not args.no_push)


if __name__ == "__main__":
    main()
