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


def run(cmd, timeout=120):
    """한글 경로/출력 때문에 cp949 로 깨지지 않도록 항상 utf-8 로 읽는다."""
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def crawler_running() -> bool:
    """crawl_data_go_kr.py 프로세스가 살아 있는지 확인.

    확인에 실패하면 '살아 있다'로 본다 - 잘못 죽었다고 판단해 미완성 상태로
    커밋해 버리는 쪽이 더 나쁘기 때문이다.
    """
    try:
        r = run(["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
                 "| Select-Object -ExpandProperty CommandLine"], timeout=60)
        if r.returncode != 0 or r.stdout is None:
            return True
        return "crawl_data_go_kr.py" in r.stdout
    except Exception:
        return True


def resume_crawler(workers: int) -> bool:
    """수집기가 죽어 있으면 다시 띄운다(중단 지점부터 이어받는다)."""
    log_path = os.path.join(ROOT, "crawl_previews.log")
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n=== %s 재개 ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.flush()
            subprocess.Popen([sys.executable, "crawl_data_go_kr.py", "--previews", "--workers", str(workers)],
                             cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"), creationflags=flags)
        log("수집기를 다시 실행했습니다 (로그: crawl_previews.log)")
        return True
    except Exception as e:
        log("수집기 재실행 실패: %r" % e)
        return False


def wait_until_done(interval: int, stall_minutes: int, resume: bool, workers: int, max_resume: int) -> dict:
    last_done, stall_since, resumes = None, time.time(), 0
    while True:
        try:
            c = counts()
        except Exception as e:  # DB 잠금 등 - 정체로 오해하지 않도록 시계도 미룬다
            log("진행 상황 확인 실패(%s) - %d초 후 재시도" % (e.__class__.__name__, interval))
            stall_since = time.time()
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

        alive = crawler_running()
        idle_min = (time.time() - stall_since) / 60

        # 수집기가 죽었는데 남은 게 있으면 먼저 되살려 본다.
        if not alive and resume and resumes < max_resume:
            log("수집기가 보이지 않습니다 (대기 %d건 남음) - 재개를 시도합니다 [%d/%d]"
                % (c["pending"], resumes + 1, max_resume))
            if resume_crawler(workers):
                resumes += 1
                stall_since = time.time()
                time.sleep(interval)
                continue

        if idle_min >= stall_minutes and not alive:
            log("수집기가 종료되었고 %.0f분간 진행이 없어 여기까지를 결과로 올립니다. (대기 %d건 남음)"
                % (idle_min, c["pending"]))
            return c
        time.sleep(interval)


def git(*args, check=True):
    r = run(["git"] + list(args))
    if check and r.returncode != 0:
        raise RuntimeError("git %s 실패: %s" % (" ".join(args), ((r.stderr or r.stdout) or "").strip()[:400]))
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

    state = "수집 완료" if c["pending"] == 0 else "중단 시점 (%s건 남음)" % format(c["pending"], ",")
    message = (
        "데이터 스냅샷 갱신 - 공공데이터 미리보기 %s/%s건 · %s\n\n"
        "- 미리보기 처리 %s건 (ok %s / 없음 %s / 대상아님 %s / 오류 %s / 대기 %s)\n"
        "- 카탈로그 %s건 (AI Hub + 공공데이터포털)\n"
        "- data/snapshot 재생성, catalog.db 는 용량 때문에 계속 제외\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
        % (c["done"], c["total"], state, c["done"], c["raw"].get("ok", 0), c["raw"].get("none", 0),
           c["raw"].get("not_applicable", 0), c["raw"].get("error", 0), c["pending"], c["total"] + 975)
    )
    git("commit", "-m", message)
    log("커밋 완료: %s" % ((git("log", "-1", "--oneline").stdout or "").strip()))

    if not push:
        log("--no-push 이므로 푸시하지 않습니다.")
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    r = subprocess.run(["git", "push", "origin", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if r.returncode == 0:
        log("푸시 완료 → %s" % ((git("remote", "get-url", "origin").stdout or "").strip()))
    else:
        log("푸시 실패: %s" % ((r.stderr or r.stdout) or "").strip()[:400])
        log("원격이 앞서 있으면 `git pull --rebase` 후 `git push` 를 실행하세요.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="진행 확인 주기(초)")
    ap.add_argument("--stall-minutes", type=int, default=20, help="이만큼 진행이 없고 수집기도 없으면 완료로 간주")
    ap.add_argument("--now", action="store_true", help="기다리지 않고 즉시 실행")
    ap.add_argument("--no-push", action="store_true", help="커밋만 하고 푸시는 생략")
    ap.add_argument("--no-resume", action="store_true", help="수집기가 죽어도 다시 실행하지 않음")
    ap.add_argument("--workers", type=int, default=20, help="수집기를 재개할 때 쓸 동시 실행 수")
    ap.add_argument("--max-resume", type=int, default=8, help="수집기 자동 재개 최대 횟수")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        log("catalog.db 가 없습니다.")
        sys.exit(1)
    c = counts() if args.now else wait_until_done(
        args.interval, args.stall_minutes, not args.no_resume, args.workers, args.max_resume)
    publish(c, push=not args.no_push)


if __name__ == "__main__":
    main()
