"""브라우저 화면(로컬 웹 앱).

실행:  python main.py --web   →  http://localhost:8000

파이썬 기본 라이브러리(http.server)만 사용하므로 추가 설치가 필요 없다.
화면(static/index.html)은 아래 JSON API를 호출하고, 계획 수립은 터미널과 같은 실행기(runner.py)를 사용한다.

  GET  /api/week?date=YYYY-MM-DD   해당 주의 시간표 블록 (수업, 약속, 이동, 과제 계획)
  GET  /api/assignments            선택할 수 있는 과제 파일
  POST /api/upload?name=파일명      과제 파일 업로드 (본문 = 파일 내용, 드래그 앤 드롭에서 사용)
  POST /api/inspect                과제 파일을 미리 읽어 마감·설명 입력이 필요한지 확인
  POST /api/run                    계획 수립 시작 (백그라운드 실행)
  GET  /api/run?since=N            진행 이벤트와 결과 조회 (화면이 1초마다 호출)
  GET/POST/DELETE /api/weekly      매주 반복 일정 조회 / 추가 / 삭제 (시작일, 반복 횟수)
  GET/POST/DELETE /api/events      일회성 약속 조회 / 추가 / 삭제
  GET/DELETE      /api/plans       저장된 계획 조회 / 삭제
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import rescheduling
from .config import has_api_key
from .context import PlanContext
from .event_store import EventStore
from .plan_store import PRIORITIES, PlanStore, session_hours
from .runner import missing_inputs, run_planning
from .schedule import Schedule
from .timetable_store import TimetableStore
from .utils import KOR_WEEKDAYS, contains_date, parse_user_deadline, to_minutes
from .week_view import collect_blocks, week_of

STATIC_DIR = Path(__file__).resolve().parent / "static"
ASSIGNMENT_SUFFIXES = {".pdf", ".txt", ".md"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class PlannerApp:
    """화면이 호출하는 기능. HTTP와 분리해 두어 테스트하기 쉽다."""

    def __init__(self, base: Path, timetable: str | Path, events: str | Path, plans: str | Path, output: str | Path):
        self.data_dir = Path(base) / "data"
        self.upload_dir = self.data_dir / "uploads"
        self.timetable_path = Path(timetable)
        self.events_path = Path(events)
        self.plans_path = Path(plans)
        self.output_dir = Path(output)
        self.job: dict | None = None
        self.lock = threading.Lock()

    # ----- 데이터 접근 ---------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    def _stores(self) -> tuple[PlanStore, EventStore]:
        return PlanStore(self.plans_path), EventStore(self.events_path)

    def _timetable(self, events: EventStore) -> dict:
        return events.merged_into(TimetableStore(self.timetable_path).data)  # 시간표 파일이 없으면 빈 시간표

    def _assignment_path(self, name: str) -> Path:
        """화면에서 넘어온 파일 이름을 data/ 또는 data/uploads/ 안의 파일로만 해석한다."""
        safe = Path(name).name
        for folder in (self.upload_dir, self.data_dir):
            if (folder / safe).is_file() and (folder / safe).suffix.lower() in ASSIGNMENT_SUFFIXES:
                return folder / safe
        raise ApiError(f"과제 파일을 찾을 수 없습니다: {safe}", 404)

    # ----- 주간 시간표 ---------------------------------------------------------
    def week(self, target: str | None) -> dict:
        try:
            day = date.fromisoformat(target) if target else date.today()
        except ValueError as e:
            raise ApiError(f"날짜 형식 오류: {target}") from e
        plans, events = self._stores()
        timetable = self._timetable(events)
        days = week_of(day)
        monday = datetime.combine(days[0], datetime.min.time())
        schedule = Schedule(timetable, monday, {
            d: [(to_minutes(s), to_minutes(e), n) for s, e, n in items]
            for d, items in plans.busy_by_date("", days[0]).items()
        })
        return {
            "today": date.today().isoformat(),
            "day_start": schedule.day_start,
            "day_end": schedule.day_end,
            "meals": [{"name": m.name, "start": m.start, "end": m.end} for m in schedule.meals],
            "days": [{"date": d.isoformat(), "label": f"{d.month}/{d.day}", "weekday": KOR_WEEKDAYS[d.weekday()],
                      "free_hours": round(sum(e - s for s, e in schedule.free_slots(d)) / 60, 1)} for d in days],
            "has_api_key": has_api_key(),
            "blocks": [{"date": b.day.isoformat(), "start": b.start, "end": b.end, "name": b.name,
                        "title": b.title or b.name, "kind": b.kind, "color": b.html_color}
                       for b in collect_blocks(timetable, plans, days)],
        }

    # ----- 과제 파일 -----------------------------------------------------------
    def assignments(self) -> dict:
        files = []
        for folder in (self.upload_dir, self.data_dir):
            if folder.is_dir():
                files += [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in ASSIGNMENT_SUFFIXES]
        seen, names = set(), []
        for p in files:
            if p.name not in seen:
                seen.add(p.name)
                names.append(p.name)
        return {"files": names}

    def upload(self, name: str, content: bytes) -> dict:
        safe = re.sub(r"[^\w.\-가-힣 ()]", "_", Path(name).name).strip() or "assignment.pdf"
        if Path(safe).suffix.lower() not in ASSIGNMENT_SUFFIXES:
            raise ApiError("PDF, TXT, MD 파일만 올릴 수 있습니다.")
        if not content:
            raise ApiError("빈 파일입니다.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ApiError("파일이 너무 큽니다. (최대 20MB)")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / safe).write_bytes(content)
        return {"name": safe, **self.inspect({"assignment": safe})}

    def inspect(self, body: dict) -> dict:
        """과제 파일을 미리 읽어, 사용자가 마감이나 설명을 입력해야 하는지 알려준다."""
        path = self._assignment_path(str(body.get("assignment", "")))
        plans, events = self._stores()
        ctx = PlanContext(path, self._timetable(events), self._now(), verbose=False, plan_store=plans)
        try:
            text = ctx.extract_text()
        except (OSError, ValueError) as e:
            raise ApiError(str(e)) from e
        sparse = ctx.text_is_sparse()
        return {
            "sparse": sparse,  # 문제가 이미지로 된 PDF 등 → 과제 설명 필요
            "has_date": (not sparse) and contains_date(text),  # False면 마감 입력 필요
            "chars": len(text),
            "has_saved_plan": plans.find(path.name) is not None,
        }

    # ----- 계획 수립 실행 -------------------------------------------------------
    def start_run(self, body: dict) -> dict:
        with self.lock:
            if self.job and not self.job["done"]:
                raise ApiError("이미 실행 중인 계획이 있습니다. 끝난 뒤 다시 시도하세요.", 409)
            path = self._assignment_path(str(body.get("assignment", "")))
            priority = body.get("priority") or "normal"
            if priority not in PRIORITIES:
                raise ApiError(f"우선순위는 {', '.join(PRIORITIES)} 중 하나여야 합니다.")
            plans, events = self._stores()
            now = self._now()
            ctx = PlanContext(path, self._timetable(events), now, output_dir=self.output_dir, verbose=True,
                              plan_store=plans, save_plan=bool(body.get("save", True)), priority=priority)
            ctx.defer_logs = True  # 터미널에 찍지 않고 화면 이벤트로 보낸다
            ctx.user_note = str(body.get("note") or "").strip()
            if body.get("deadline"):
                try:
                    ctx.user_deadline = parse_user_deadline(str(body["deadline"]), now)
                except ValueError as e:
                    raise ApiError(str(e), needs=["deadline"]) from e
                if ctx.user_deadline <= now:
                    raise ApiError("마감이 현재 시각보다 이전입니다.", needs=["deadline"])
            missing = missing_inputs(ctx)
            if missing:
                labels = {"note": "과제 설명(문서 내용을 읽을 수 없음)", "deadline": "마감일(문서에 날짜가 없음)"}
                raise ApiError("추가 입력이 필요합니다: " + ", ".join(labels[m] for m in missing), needs=missing)

            job = {"id": now.strftime("%Y%m%d%H%M%S"), "assignment": path.name, "events": [], "done": False, "summary": None}
            self.job = job
            threading.Thread(target=self._run_job, args=(job, ctx), daemon=True).start()
            return {"run_id": job["id"], "assignment": path.name}

    def _run_job(self, job: dict, ctx: PlanContext) -> None:
        try:
            job["summary"] = asyncio.run(run_planning(ctx, job["events"].append))
        except Exception as e:  # 실행기 밖의 예기치 못한 오류도 화면에 알린다
            job["events"].append({"type": "log", "text": f"실행 중 오류: {type(e).__name__}: {e}"})
            job["summary"] = {"status": "failed", "failure_reason": str(e), "files": [], "postponed": [],
                              "attempts": ctx.attempts, "max_attempts": ctx.max_attempts,
                              "reanalysis": ctx.reanalysis_count, "max_reanalysis": ctx.max_reanalysis,
                              "tokens_in": 0, "tokens_out": 0, "messages": 0, "stop_reason": None}
        finally:
            job["done"] = True

    def run_status(self, since: int) -> dict:
        job = self.job
        if job is None:
            return {"run_id": None, "events": [], "next": 0, "done": True, "summary": None}
        events = job["events"][since:]
        return {"run_id": job["id"], "assignment": job["assignment"], "events": events, "next": since + len(events),
                "done": job["done"], "summary": job["summary"]}

    # ----- 약속 ---------------------------------------------------------------
    def list_events(self) -> dict:
        _, events = self._stores()
        return {"events": events.upcoming(date.today())}

    def add_event(self, body: dict) -> dict:
        plans, events = self._stores()
        try:
            event = events.add(str(body.get("name", "")), str(body.get("date", "")),
                               str(body.get("start", "")), str(body.get("end", "")))
        except ValueError as e:
            raise ApiError(f"약속을 추가할 수 없습니다: {e}") from e
        events.save()
        moved, unresolved = rescheduling.repair_conflicts(plans, self._timetable(events), self._now())
        return {"event": event, "moved": moved, "unresolved": unresolved}

    def remove_event(self, event_id: str) -> dict:
        _, events = self._stores()
        removed = events.remove(event_id)
        if not removed:
            raise ApiError("해당 약속이 없습니다.", 404)
        events.save()
        return {"removed": removed}

    # ----- 매주 반복 일정 --------------------------------------------------------
    def list_weekly(self) -> dict:
        return {"weekly": TimetableStore(self.timetable_path).list_weekly(date.today())}

    def add_weekly(self, body: dict) -> dict:
        plans, events = self._stores()
        timetable = TimetableStore(self.timetable_path)
        try:
            count = body.get("count")
            entry = timetable.add_weekly(
                str(body.get("name", "")), str(body.get("day", "")), str(body.get("start", "")), str(body.get("end", "")),
                count=int(count) if count not in (None, "") else None, start_date=body.get("from") or None,
                short=str(body.get("short") or ""), at_school=bool(body.get("at_school", True)))
        except (ValueError, TypeError) as e:
            raise ApiError(f"반복 일정을 추가할 수 없습니다: {e}") from e
        timetable.save()
        moved, unresolved = rescheduling.repair_conflicts(plans, self._timetable(events), self._now())
        return {"entry": timetable.describe(entry, date.today()), "moved": moved, "unresolved": unresolved}

    def remove_weekly(self, entry_id: str) -> dict:
        timetable = TimetableStore(self.timetable_path)
        removed = timetable.remove_weekly(entry_id)
        if not removed:
            raise ApiError("해당 반복 일정이 없습니다.", 404)
        timetable.save()
        return {"removed": removed["id"]}

    # ----- 저장된 계획 ----------------------------------------------------------
    def list_plans(self) -> dict:
        plans, _ = self._stores()
        today = date.today().isoformat()
        out = []
        for p in plans.plans:
            days = sorted({s["date"] for s in p["sessions"]})
            out.append({
                "id": p["id"], "assignment": p["assignment"], "deadline": p["deadline"],
                "priority": p.get("priority", "normal"), "sessions": len(p["sessions"]),
                "hours": round(sum(session_hours(s) for s in p["sessions"]), 1),
                "remaining_hours": round(sum(session_hours(s) for s in p["sessions"] if s["date"] >= today), 1),
                "span": f"{days[0][5:]} ~ {days[-1][5:]}" if days else "-",
                "rescheduled": bool(p.get("rescheduled")),
            })
        return {"plans": out}

    def remove_plan(self, plan_id: str) -> dict:
        plans, _ = self._stores()
        removed = plans.remove(plan_id)
        if not removed:
            raise ApiError("해당 계획이 없습니다.", 404)
        plans.save()
        return {"removed": removed["id"]}


def make_handler(app: PlannerApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 요청마다 찍히는 접근 로그를 끈다
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD_BYTES + 1024:
                raise ApiError("요청이 너무 큽니다.", 413)
            return self.rfile.read(length) if length else b""

        def _json_body(self) -> dict:
            raw = self._body()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as e:
                raise ApiError("JSON 형식이 잘못되었습니다.") from e
            if not isinstance(data, dict):
                raise ApiError("JSON 객체가 필요합니다.")
            return data

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path.rstrip("/") or "/"
            try:
                if method == "GET" and path == "/":
                    return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if method == "GET" and path == "/api/week":
                    return self._json(app.week(query.get("date")))
                if method == "GET" and path == "/api/assignments":
                    return self._json(app.assignments())
                if method == "POST" and path == "/api/upload":
                    return self._json(app.upload(unquote(query.get("name", "")), self._body()))
                if method == "POST" and path == "/api/inspect":
                    return self._json(app.inspect(self._json_body()))
                if method == "POST" and path == "/api/run":
                    return self._json(app.start_run(self._json_body()))
                if method == "GET" and path == "/api/run":
                    return self._json(app.run_status(int(query.get("since", 0))))
                if method == "GET" and path == "/api/events":
                    return self._json(app.list_events())
                if method == "POST" and path == "/api/events":
                    return self._json(app.add_event(self._json_body()))
                if method == "DELETE" and path.startswith("/api/events/"):
                    return self._json(app.remove_event(unquote(path.rsplit("/", 1)[1])))
                if method == "GET" and path == "/api/weekly":
                    return self._json(app.list_weekly())
                if method == "POST" and path == "/api/weekly":
                    return self._json(app.add_weekly(self._json_body()))
                if method == "DELETE" and path.startswith("/api/weekly/"):
                    return self._json(app.remove_weekly(unquote(path.rsplit("/", 1)[1])))
                if method == "GET" and path == "/api/plans":
                    return self._json(app.list_plans())
                if method == "DELETE" and path.startswith("/api/plans/"):
                    return self._json(app.remove_plan(unquote(path.rsplit("/", 1)[1])))
                self._json({"error": "없는 주소입니다."}, 404)
            except ApiError as e:
                self._json(e.payload, e.status)
            except (OSError, ValueError) as e:
                self._json({"error": str(e)}, 400)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def serve(base: Path, port: int = 8000, **paths) -> None:
    """웹 화면 서버를 실행한다. 이 컴퓨터에서만 접속할 수 있다(127.0.0.1)."""
    logging.getLogger("autogen_core").setLevel(logging.CRITICAL)
    app = PlannerApp(base, **paths)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError as e:
        print(f"포트 {port}을(를) 열 수 없습니다 ({e.strerror}). 이미 실행 중인 화면이 있는지 확인하거나 "
              f"다른 포트로 실행하세요: python main.py --web {port + 1}")
        return
    print(f"Assignment Planner 화면: http://localhost:{port}   (종료: Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()


# ----- 앱 창으로 띄우기 -------------------------------------------------------------
def _is_wsl() -> bool:
    import platform

    return "microsoft" in platform.release().lower()


def find_app_browser() -> str | None:
    """앱 모드(--app)를 지원하는 Edge/Chrome 실행 파일을 찾는다. Windows, WSL, macOS, Linux 순으로 확인."""
    import os
    import shutil
    import sys

    candidates: list[str] = []
    if sys.platform == "win32" or _is_wsl():
        roots = ([os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
                 if sys.platform == "win32" else ["/mnt/c/Program Files (x86)", "/mnt/c/Program Files"])
        for root in filter(None, roots):
            candidates += [str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                           str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return next((c for c in candidates if Path(c).is_file()), None)


def _app_profile_dir() -> str | None:
    """앱 창 전용 브라우저 프로필 폴더. 전용 프로필로 띄워야 창이 닫히는 시점을 알 수 있다."""
    import subprocess
    import tempfile

    if _is_wsl():  # Windows 브라우저를 띄우므로 Windows 쪽 경로가 필요하다
        try:
            out = subprocess.run(["cmd.exe", "/c", "echo %LOCALAPPDATA%"], capture_output=True, timeout=10, cwd="/mnt/c")
            local = out.stdout.decode("utf-8", "ignore").strip()
            return local + "\\AssignmentPlanner" if local and "%" not in local else None
        except (OSError, subprocess.SubprocessError):
            return None
    return str(Path(tempfile.gettempdir()) / "assignment-planner-profile")


def serve_app(base: Path, **paths) -> None:
    """화면을 주소창·탭이 없는 독립된 앱 창으로 띄운다. 창을 닫으면 프로그램도 끝난다."""
    import subprocess
    import webbrowser

    logging.getLogger("autogen_core").setLevel(logging.CRITICAL)
    app = PlannerApp(base, **paths)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))  # 빈 포트를 자동으로 고른다
    url = f"http://localhost:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    browser, profile = find_app_browser(), _app_profile_dir()
    try:
        if browser and profile:
            print("Assignment Planner 창을 엽니다. 창을 닫으면 프로그램이 종료됩니다.")
            subprocess.run([browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,960",
                            "--no-first-run", "--no-default-browser-check"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while app.job and not app.job["done"]:  # 실행 중에 창을 닫았으면 결과 저장까지 기다린다
                threading.Event().wait(1)
        else:
            if browser:
                subprocess.Popen([browser, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                print("Edge/Chrome을 찾지 못해 기본 브라우저로 엽니다.")
                webbrowser.open(url)
            print(f"화면 주소: {url}   (종료: Ctrl+C)")
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("프로그램을 종료합니다.")
        server.shutdown()
        server.server_close()
