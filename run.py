# -*- coding: utf-8 -*-
"""수집부터 배포까지 한 번에 처리하는 실행기.

    python run.py all       수집 -> 스냅샷 -> 빌드 -> GitHub -> Pages 배포
    python run.py crawl     수집만 (AI Hub + 공공데이터포털)
    python run.py publish   스냅샷 -> 빌드 -> GitHub -> Pages 배포
    python run.py snapshot  스냅샷만
    python run.py build     정적 사이트 빌드만
    python run.py git       GitHub 업로드만
    python run.py deploy    Cloudflare Pages 배포만
    python run.py serve     로컬 대시보드 실행
    python run.py status    현재 수집/스냅샷 현황

한글 안내를 모두 이 파일에서 출력한다. 배치 파일에는 ASCII 만 남겨 두어
cmd.exe 가 한글 배치 파일을 잘못 읽고 중간에 멈추는 문제를 피한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGES_PROJECT = "publicdata-finder"
GIT_REMOTES = ("origin", "finder")      # 없는 원격은 건너뛴다
SITE_DIR = os.path.join(ROOT, "site")


# --------------------------------------------------------------------------- 출력
def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def head(title: str) -> None:
    _out("")
    _out("=" * 64)
    _out("  " + title)
    _out("=" * 64)


def info(text: str) -> None:
    _out("  " + text)


def fail(text: str) -> None:
    _out("")
    _out("  [실패] " + text)


def took(seconds: float) -> str:
    if seconds < 60:
        return "%.0f초" % seconds
    if seconds < 3600:
        return "%d분 %d초" % (seconds // 60, seconds % 60)
    return "%d시간 %d분" % (seconds // 3600, (seconds % 3600) // 60)


class StepError(Exception):
    """한 단계가 실패했을 때. 메시지는 사용자에게 그대로 보여 준다."""


# --------------------------------------------------------------------------- 실행 도우미
def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 잘못 남아 있는 토큰이 wrangler 로그인보다 우선해 배포가 막히는 일이 있다.
    for key in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        if not env.get(key):
            env.pop(key, None)
    return env


def run_live(args, what: str) -> None:
    """자식 출력을 그대로 흘려보낸다. 오래 걸리는 단계용."""
    try:
        code = subprocess.call(args, cwd=ROOT, env=child_env())
    except FileNotFoundError:
        raise StepError("%s 실행 파일을 찾지 못했습니다: %s" % (what, args[0]))
    except KeyboardInterrupt:
        raise StepError("%s 도중 사용자가 중단했습니다." % what)
    if code != 0:
        raise StepError("%s 이(가) 오류로 끝났습니다. (종료 코드 %d)" % (what, code))


def run_capture(args, what: str, check: bool = True):
    """출력을 받아 온다. git 처럼 결과를 봐야 하는 단계용."""
    try:
        proc = subprocess.run(args, cwd=ROOT, env=child_env(), capture_output=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise StepError("%s 실행 파일을 찾지 못했습니다: %s" % (what, args[0]))
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if check and proc.returncode != 0:
        raise StepError("%s 실패 (종료 코드 %d)\n%s" % (what, proc.returncode, text[-1500:]))
    return proc.returncode, text


def py(*args) -> list:
    return [sys.executable] + list(args)


def npx_cmd() -> str:
    """Windows 의 cmd 에서는 npx.cmd 를 직접 가리켜야 안전하다."""
    for name in ("npx.cmd", "npx"):
        found = shutil.which(name)
        if found:
            return found
    raise StepError("npx 를 찾지 못했습니다. Node.js 가 설치되어 있는지 확인해 주세요.\n"
                    "  https://nodejs.org 에서 LTS 를 설치하면 됩니다.")


# --------------------------------------------------------------------------- 단계
def step_crawl() -> None:
    head("1단계 · 데이터 수집")
    info("AI Hub 목록을 다시 받아 새 데이터셋을 추가합니다.")
    started = time.time()
    run_live(py("crawl_aihub.py", "--refresh"), "AI Hub 수집")
    info("AI Hub 수집 완료 (%s)" % took(time.time() - started))

    _out("")
    info("공공데이터포털 목록·상세·미리보기를 수집합니다.")
    info("8만 건이 넘어 몇 시간 걸릴 수 있습니다. 중간에 끊겨도 다시 실행하면 이어집니다.")
    started = time.time()
    run_live(py("crawl_data_go_kr.py", "--list", "--catalog", "--details",
                "--missing-previews", "--previews", "--refresh-list", "--workers", "20"),
             "공공데이터포털 수집")
    info("공공데이터포털 수집 완료 (%s)" % took(time.time() - started))


def step_snapshot() -> None:
    head("2단계 · 스냅샷 만들기")
    info("catalog.db(1.3GB)는 GitHub 에 올릴 수 없어, 압축한 스냅샷으로 내보냅니다.")
    run_live(py("snapshot.py", "export"), "스냅샷 내보내기")


def step_build() -> None:
    head("3단계 · 정적 사이트 빌드")
    info("스냅샷을 Cloudflare Pages 가 쓸 JSON 샤드로 변환합니다.")
    run_live(py("build_static.py"), "정적 사이트 빌드")
    if not os.path.isfile(os.path.join(SITE_DIR, "index.html")):
        raise StepError("빌드 결과에 site/index.html 이 없습니다.")


def git_remotes() -> list:
    code, text = run_capture(["git", "remote"], "git remote", check=False)
    if code != 0:
        raise StepError("git 저장소가 아닙니다. 이 폴더에서 git init 이 되어 있는지 확인해 주세요.")
    have = {line.strip() for line in text.splitlines() if line.strip()}
    return [r for r in GIT_REMOTES if r in have]


def step_git() -> None:
    head("4단계 · GitHub 업로드")
    remotes = git_remotes()
    if not remotes:
        raise StepError("등록된 원격 저장소(origin/finder)가 없습니다.\n"
                        "  git remote add origin <저장소 주소> 로 먼저 연결해 주세요.")

    run_capture(["git", "add", "-A"], "git add")
    code, staged = run_capture(["git", "diff", "--cached", "--stat"], "git diff", check=False)
    if not staged.strip():
        info("바뀐 파일이 없어 새로 커밋하지 않습니다. 최신 커밋을 그대로 올립니다.")
    else:
        changed = len([l for l in staged.splitlines() if "|" in l])
        info("변경된 파일 %d개를 커밋합니다." % changed)
        message = "데이터 갱신 %s" % time.strftime("%Y-%m-%d %H:%M")
        run_capture(["git", "commit", "-m", message], "git commit")
        info("커밋 완료: %s" % message)

    for remote in remotes:
        info("%s 에 올리는 중..." % remote)
        code, text = run_capture(["git", "push", remote, "HEAD:main"], "git push", check=False)
        if code != 0:
            low = text.lower()
            if "authentication" in low or "could not read" in low or "403" in low:
                raise StepError("GitHub 인증에 실패했습니다.\n"
                                "  git push 를 한 번 직접 실행해 로그인한 뒤 다시 시도해 주세요.\n" + text[-600:])
            raise StepError("%s 푸시 실패\n%s" % (remote, text[-800:]))
        info("%s 완료" % remote)


def step_deploy() -> None:
    head("5단계 · Cloudflare Pages 배포")
    if not os.path.isdir(SITE_DIR):
        raise StepError("site 폴더가 없습니다. 먼저 빌드를 실행해 주세요.")

    npx = npx_cmd()
    code, who = run_capture([npx, "wrangler", "whoami"], "wrangler whoami", check=False)
    if code != 0 or "you are logged in" not in who.lower():
        raise StepError("Cloudflare 에 로그인되어 있지 않습니다.\n"
                        "  명령 프롬프트에서 npx wrangler login 을 실행해 로그인한 뒤 다시 시도해 주세요.")

    info("%s 프로젝트로 배포합니다." % PAGES_PROJECT)
    started = time.time()
    run_live([npx, "wrangler", "pages", "deploy", "site",
              "--project-name", PAGES_PROJECT, "--branch", "main", "--commit-dirty=true"],
             "Pages 배포")
    info("배포 완료 (%s)" % took(time.time() - started))
    info("주소: https://%s.pages.dev" % PAGES_PROJECT)


def step_serve() -> None:
    head("로컬 대시보드")
    info("이 창을 닫으면 서버가 멈춥니다. 브라우저가 자동으로 열립니다.")
    run_live(py("serve.py"), "대시보드 서버")


def step_status() -> None:
    head("현재 현황")
    run_live(py("snapshot.py", "info"), "스냅샷 현황")
    _out("")
    run_live(py("crawl_data_go_kr.py", "--status"), "수집 현황")


# --------------------------------------------------------------------------- 메뉴
MENU = [
    ("1", "publish", "배포하기  (스냅샷 → 빌드 → GitHub → Pages)", "가장 자주 쓰는 작업. 몇 분이면 끝납니다."),
    ("2", "all", "전체 실행  (수집부터 배포까지 전부)", "수집이 포함되어 몇 시간 걸릴 수 있습니다."),
    ("3", "crawl", "데이터 수집만", "AI Hub + 공공데이터포털을 새로 받아옵니다."),
    ("4", "serve", "로컬 대시보드 열기", "내 컴퓨터에서 대시보드를 띄웁니다."),
    ("5", "status", "현황 보기", "수집이 어디까지 됐는지 확인합니다."),
    ("6", "deploy", "Pages 배포만", "이미 빌드된 site 폴더를 그대로 올립니다."),
    ("7", "git", "GitHub 업로드만", "바뀐 파일을 커밋하고 올립니다."),
]


def show_menu() -> None:
    _out("")
    _out("=" * 64)
    _out("  공공데이터 카탈로그 · 무엇을 할까요?")
    _out("=" * 64)
    for key, _task, title, hint in MENU:
        _out("   [%s] %s" % (key, title))
        _out("       %s" % hint)
    _out("   [0] 끝내기")
    _out("")


def run_menu() -> int:
    choices = {key: task for key, task, _t, _h in MENU}
    while True:
        show_menu()
        try:
            picked = input("  번호를 입력하고 Enter: ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("")
            return 0
        if picked in ("0", "q", "Q"):
            return 0
        task = choices.get(picked)
        if not task:
            info("1~%d 사이의 번호나 0(끝내기)을 입력해 주세요." % len(MENU))
            continue
        run_task(task)
        _out("")
        try:
            input("  메뉴로 돌아가려면 Enter 를 누르세요...")
        except (EOFError, KeyboardInterrupt):
            return 0


# --------------------------------------------------------------------------- 진입점
TASKS = {
    "crawl":    ("데이터 수집", [step_crawl]),
    "snapshot": ("스냅샷", [step_snapshot]),
    "build":    ("정적 사이트 빌드", [step_build]),
    "git":      ("GitHub 업로드", [step_git]),
    "deploy":   ("Pages 배포", [step_deploy]),
    "publish":  ("배포 (스냅샷 -> 빌드 -> GitHub -> Pages)",
                 [step_snapshot, step_build, step_git, step_deploy]),
    "all":      ("전체 (수집 -> 스냅샷 -> 빌드 -> GitHub -> Pages)",
                 [step_crawl, step_snapshot, step_build, step_git, step_deploy]),
    "serve":    ("로컬 대시보드", [step_serve]),
    "status":   ("현황 보기", [step_status]),
}


def run_task(task: str) -> int:
    label, steps = TASKS[task]
    started = time.time()
    _out("")
    _out("  작업: %s" % label)
    _out("  폴더: %s" % ROOT)

    try:
        for step in steps:
            step()
    except StepError as exc:
        fail(str(exc))
        _out("")
        info("문제를 해결한 뒤 다시 실행하면 끝난 단계는 건너뛰고 이어집니다.")
        return 1
    except KeyboardInterrupt:
        _out("")
        fail("사용자가 중단했습니다.")
        return 130

    if task not in ("serve", "status"):
        head("완료 · 총 %s" % took(time.time() - started))
        if step_deploy in steps:
            info("사이트: https://%s.pages.dev" % PAGES_PROJECT)
    return 0


def main(argv) -> int:
    task = (argv[1] if len(argv) > 1 else "menu").strip().lower()
    if task in ("-h", "--help", "help"):
        _out(__doc__)
        return 0
    if task == "menu":
        return run_menu()
    if task not in TASKS:
        fail("알 수 없는 작업입니다: %s" % task)
        info("쓸 수 있는 작업: " + ", ".join(TASKS))
        return 2
    return run_task(task)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
