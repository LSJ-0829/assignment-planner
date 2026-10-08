"""일회성 약속, 약속과 겹친 계획 자동 조정, 웹 화면 API 테스트 (API 키 불필요)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest
from conftest import START, TIMETABLE

from planner_system import rescheduling
from planner_system.event_store import EventStore, parse_event
from planner_system.plan_store import PlanStore
from planner_system.schedule import Schedule
from planner_system.webapp import ApiError, PlannerApp, make_handler


def saved_plan(tmp_path, deadline="2026-10-05 23:59"):
    store = PlanStore(tmp_path / "plans.json")
    store.upsert("hw.pdf", [
        {"task_id": "T1", "title": "풀이", "date": "2026-10-01", "start": "18:00", "end": "20:00", "deadline": deadline},
        {"task_id": "T2", "title": "정리", "date": "2026-10-02", "start": "09:00", "end": "11:00", "deadline": deadline},
    ], datetime.strptime(deadline, "%Y-%m-%d %H:%M"))
    store.save()
    return store


# ----- 약속 저장소 --------------------------------------------------------------
def test_parse_event_formats():
    now = datetime(2026, 9, 28, 18, 0)
    assert parse_event("10/9 18:00-20:00 저녁 약속", now) == {
        "name": "저녁 약속", "date": "2026-10-09", "start": "18:00", "end": "20:00"}
    assert parse_event("2026-10-09 9:00~10:30 병원", now)["start"] == "9:00"
    assert parse_event("10월 9일 18:00-20:00 동아리 모임", now)["name"] == "동아리 모임"
    assert parse_event("9/28 19:00-20:00 오늘 약속", now)["date"] == "2026-09-28"  # 오늘 날짜도 올해로 해석
    with pytest.raises(ValueError):
        parse_event("저녁 약속", now)


def test_event_store_add_remove_and_merge(tmp_path):
    store = EventStore(tmp_path / "events.json")
    event = store.add("저녁 약속", "2026-10-01", "18:00", "20:00")
    store.save()
    with pytest.raises(ValueError):
        store.add("거꾸로", "2026-10-01", "20:00", "18:00")
    with pytest.raises(ValueError):
        store.add("", "2026-10-01", "18:00", "20:00")

    reloaded = EventStore(tmp_path / "events.json")
    assert [e["name"] for e in reloaded.events] == ["저녁 약속"]
    merged = reloaded.merged_into(TIMETABLE)
    assert len(merged["events"]) == len(TIMETABLE["events"]) + 1 and len(TIMETABLE["events"]) == 1  # 원본은 그대로
    # 합쳐진 시간표에서는 그 시간이 빈 시간에서 빠진다
    free = Schedule(merged, START).free_slots(date(2026, 10, 1))
    assert all(not (s < 20 * 60 and e > 18 * 60) for s, e in free)
    assert reloaded.remove(event["id"]) and not reloaded.events


# ----- 약속과 겹친 계획 자동 조정 --------------------------------------------------
def test_new_event_moves_conflicting_plan_sessions(tmp_path):
    plans = saved_plan(tmp_path)
    events = EventStore(tmp_path / "events.json")
    events.add("저녁 약속", "2026-10-01", "18:00", "20:00")  # 계획의 T1 세션과 겹친다
    moved, unresolved = rescheduling.repair_conflicts(plans, events.merged_into(TIMETABLE), START)
    assert moved == ["hw · 풀이: 10-01 18:00-20:00 → 10-01 20:00-22:00"] and not unresolved
    saved = PlanStore(tmp_path / "plans.json").find("hw.pdf")["sessions"]
    assert (saved[0]["start"], saved[0]["end"]) == ("20:00", "22:00")
    assert (saved[1]["date"], saved[1]["start"]) == ("2026-10-02", "09:00")  # 겹치지 않는 세션은 그대로


def test_conflict_reported_when_no_room_before_deadline(tmp_path):
    plans = saved_plan(tmp_path, deadline="2026-10-01 20:00")
    events = EventStore(tmp_path / "events.json")
    events.add("저녁 약속", "2026-10-01", "18:00", "20:00")
    moved, unresolved = rescheduling.repair_conflicts(plans, events.merged_into(TIMETABLE), START)
    assert not moved and "옮길 빈 시간이 없습니다" in unresolved[0]
    assert PlanStore(tmp_path / "plans.json").find("hw.pdf")["sessions"][0]["start"] == "18:00"  # 그대로 둔다


# ----- 웹 화면 API ---------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "timetable.json").write_text(json.dumps(TIMETABLE, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "data" / "dated.txt").write_text("과제: 구현과 리포트 작성. 마감 2026-10-02 23:59까지 제출", encoding="utf-8")
    return PlannerApp(tmp_path, tmp_path / "data" / "timetable.json", tmp_path / "data" / "events.json",
                      tmp_path / "data" / "plans.json", tmp_path / "out")


def test_app_week_events_and_plans(app):
    week = app.week("2026-09-30")
    assert [d["date"] for d in week["days"]][0] == "2026-09-28" and len(week["days"]) == 7
    assert {"월요수업", "화요수업", "약속"} <= {b["name"] for b in week["blocks"]}

    added = app.add_event({"name": "병원", "date": "2026-10-01", "start": "10:00", "end": "11:00"})
    assert added["moved"] == [] and added["unresolved"] == []
    assert any(b["name"] == "병원" and b["kind"] == "event" for b in app.week("2026-10-01")["blocks"])
    with pytest.raises(ApiError):
        app.add_event({"name": "잘못된 시각", "date": "2026-10-01", "start": "11:00", "end": "10:00"})
    assert app.remove_event(added["event"]["id"])["removed"]["name"] == "병원"
    assert app.list_plans() == {"plans": []}


def test_app_upload_inspect_and_input_checks(app):
    info = app.upload("../../숙제 3.pdf.txt", "1.\n2.".encode("utf-8"))  # 경로 조작은 파일 이름만 남긴다
    assert info["name"] == "숙제 3.pdf.txt" and info["sparse"] and not info["has_date"]
    assert (app.upload_dir / "숙제 3.pdf.txt").is_file()
    assert app.inspect({"assignment": "dated.txt"})["has_date"]
    assert set(app.assignments()["files"]) == {"숙제 3.pdf.txt", "dated.txt"}

    with pytest.raises(ApiError):
        app.upload("virus.exe", b"x")
    with pytest.raises(ApiError) as err:  # 읽을 수 없는 문서 → 설명과 마감이 필요
        app.start_run({"assignment": "숙제 3.pdf.txt"})
    assert set(err.value.payload["needs"]) == {"note", "deadline"}
    with pytest.raises(ApiError):
        app.start_run({"assignment": "없는파일.pdf"})
    assert app.run_status(0)["run_id"] is None  # 입력이 부족하면 실행을 시작하지 않는다


def test_http_round_trip(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, method="GET", data=None):
        req = urllib.request.Request(base + path, data=data, method=method)
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read()

    try:
        status, page = call("/")
        assert status == 200 and "과제 PDF를 여기에 끌어다 놓으세요" in page.decode("utf-8")
        assert len(json.loads(call("/api/week?date=2026-09-30")[1])["days"]) == 7

        name = urllib.request.quote("끌어다놓은 과제.txt")
        uploaded = json.loads(call(f"/api/upload?name={name}", "POST", "과제: 구현과 리포트를 작성해 2026-10-02 23:59까지 제출".encode("utf-8"))[1])
        assert uploaded["name"] == "끌어다놓은 과제.txt" and uploaded["has_date"]

        future = (date.today() + timedelta(days=3)).isoformat()  # 목록에는 오늘 이후의 약속만 나온다
        body = json.dumps({"name": "약속", "date": future, "start": "18:00", "end": "19:00"}).encode("utf-8")
        event = json.loads(call("/api/events", "POST", body)[1])["event"]
        assert [e["id"] for e in json.loads(call("/api/events")[1])["events"]] == [event["id"]]
        assert call(f"/api/events/{event['id']}", "DELETE")[0] == 200
        assert json.loads(call("/api/events")[1])["events"] == []

        with pytest.raises(urllib.error.HTTPError) as err:
            call("/api/nothing")
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
