"""시간표 없이 시작하기, 매주 반복 일정 추가·삭제, 반복 횟수 테스트 (API 키 불필요)."""
import json
from datetime import date, datetime

import pytest

from planner_system import tools
from planner_system.context import PlanContext
from planner_system.schedule import Schedule, weekly_active, weekly_last_date
from planner_system.timetable_store import TimetableStore, parse_day, parse_weekly
from planner_system.webapp import ApiError, PlannerApp


def test_parse_weekly_and_day():
    assert parse_weekly("화 18:00-21:45 인공지능프로젝트") == {
        "day": "TUE", "start": "18:00", "end": "21:45", "name": "인공지능프로젝트"}
    assert parse_weekly("FRI 9:00~10:15 교양 영어")["name"] == "교양 영어"
    assert [parse_day(x) for x in ("월", "목요일", "sat", "SUN")] == ["MON", "THU", "SAT", "SUN"]
    with pytest.raises(ValueError):
        parse_weekly("인공지능프로젝트 18:00")
    with pytest.raises(ValueError):
        parse_day("매일")


def test_starts_empty_without_timetable_file(tmp_path):
    """시간표 파일이 없어도 빈 시간표와 기본 설정으로 시작한다."""
    store = TimetableStore(tmp_path / "timetable.json")
    assert not store.exists and store.weekly == [] and store.data["preferences"]["day_end"] == "24:00"
    assert "등록된 반복 일정이 없습니다" in store.summary(date(2026, 10, 8))
    free = Schedule(store.data, datetime(2026, 10, 8, 0, 0)).free_slots(date(2026, 10, 9))
    assert free == [(9 * 60, 24 * 60)]  # 반복 일정이 없으니 하루 전체가 빈 시간 (식사 시간은 계획을 세울 때 비운다)
    assert not (tmp_path / "timetable.json").exists()  # 읽기만 해서는 파일을 만들지 않는다


def test_add_weekly_with_repeat_count(tmp_path):
    store = TimetableStore(tmp_path / "timetable.json")
    entry = store.add_weekly("인공지능프로젝트", "화", "18:00", "21:45", count=3, start_date="2026-10-05", short="인지개")
    store.save()
    assert entry["day"] == "TUE" and entry["from"] == "2026-10-05" and entry["count"] == 3

    reloaded = TimetableStore(tmp_path / "timetable.json")
    saved = reloaded.weekly[0]
    # 10/5(월)부터 3회 → 10/6, 10/13, 10/20 화요일만 해당한다
    assert weekly_last_date(saved) == date(2026, 10, 20)
    tuesdays = (date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 20), date(2026, 10, 27))
    assert [weekly_active(saved, d) for d in tuesdays] == [False, True, True, False]
    assert not weekly_active(saved, date(2026, 10, 7))  # 수요일

    info = reloaded.list_weekly(date(2026, 10, 8))[0]  # 10/8 기준: 1회 지남
    assert (info["remaining"], info["last_date"], info["weekday"]) == (2, "2026-10-20", "화")
    assert "3회 중 2회 남음 (~2026-10-20)" in reloaded.summary(date(2026, 10, 8))

    sch = Schedule(reloaded.data, datetime(2026, 10, 5, 0, 0))
    assert any(name == "인공지능프로젝트" for _, _, name in sch.busy_intervals(date(2026, 10, 13)))
    assert not sch.busy_intervals(date(2026, 10, 27))  # 횟수가 끝난 뒤에는 빈 시간


def test_weekly_validation_and_remove(tmp_path):
    store = TimetableStore(tmp_path / "timetable.json")
    with pytest.raises(ValueError):
        store.add_weekly("수업", "월", "10:00", "09:00")
    with pytest.raises(ValueError):
        store.add_weekly("수업", "월", "09:00", "10:00", count=0)
    with pytest.raises(ValueError):
        store.add_weekly("", "월", "09:00", "10:00")
    forever = store.add_weekly("아르바이트", "토", "12:00", "20:00", at_school=False)
    assert "count" not in forever and forever["at_school"] is False
    assert store.list_weekly(date(2026, 10, 8))[0]["remaining"] is None  # 횟수 없음 → 계속 반복
    assert store.remove_weekly(forever["id"]) and store.weekly == []
    assert store.remove_weekly("없는ID") is None


def test_off_campus_entry_has_no_commute(tmp_path):
    store = TimetableStore(tmp_path / "timetable.json")
    store.data["preferences"].update({"commute_before_minutes": 120, "commute_after_minutes": 60, "meals": []})
    store.add_weekly("수업", "월", "13:00", "14:00", start_date="2026-10-05")
    store.add_weekly("아르바이트", "화", "13:00", "14:00", start_date="2026-10-05", at_school=False)
    sch = Schedule(store.data, datetime(2026, 10, 5, 0, 0))
    assert [n for _, _, n in sch.busy_intervals(date(2026, 10, 5))] == ["등교 준비·이동", "수업", "귀가 이동"]
    assert [n for _, _, n in sch.busy_intervals(date(2026, 10, 6))] == ["아르바이트"]


def test_planning_respects_repeat_count(tmp_path):
    """반복 횟수가 끝난 주에는 그 시간에 과제를 배치할 수 있다."""
    store = TimetableStore(tmp_path / "timetable.json")
    store.data["preferences"]["meals"] = []
    store.add_weekly("수업", "금", "09:00", "24:00", count=1, start_date="2026-10-05")  # 10/9 하루 종일, 1회만
    p = tmp_path / "a.txt"
    p.write_text("과제: 기능 구현. 마감 2026-10-16 23:59까지 제출", encoding="utf-8")
    ctx = PlanContext(p, store.data, datetime(2026, 10, 8, 20, 0), output_dir=tmp_path / "out", verbose=False,
                      max_reanalysis=0)
    tools.submit_tasks(ctx, json.dumps([{"id": "T1", "title": "구현", "hours": 2, "deadline": "2026-10-16 23:59"}]))
    text = tools.get_free_slots(ctx)
    assert "2026-10-09(금): 없음" in text and "2026-10-16(금): 09:00-24:00" in text
    tools.submit_plan(ctx, json.dumps([{"task_id": "T1", "date": "2026-10-09", "start": "10:00", "end": "12:00"}]))
    assert "'수업'(09:00-24:00) 일정과 겹칩니다" in tools.validate_plan(ctx)
    tools.submit_plan(ctx, json.dumps([{"task_id": "T1", "date": "2026-10-16", "start": "10:00", "end": "12:00"}]))
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")


def test_web_api_weekly_without_timetable_file(tmp_path):
    app = PlannerApp(tmp_path, tmp_path / "data" / "timetable.json", tmp_path / "data" / "events.json",
                     tmp_path / "data" / "plans.json", tmp_path / "out")
    assert app.list_weekly() == {"weekly": []} and len(app.week("2026-10-08")["days"]) == 7  # 시간표 없이도 화면이 뜬다
    added = app.add_weekly({"name": "운영체제", "day": "MON", "start": "09:00", "end": "10:15", "count": "16",
                            "from": "2026-09-01", "short": "OS"})
    assert added["entry"]["count"] == 16 and added["entry"]["last_date"] == "2026-12-21"
    assert any(b["name"] == "OS" and b["title"] == "운영체제" for b in app.week("2026-10-05")["blocks"])
    with pytest.raises(ApiError):
        app.add_weekly({"name": "잘못", "day": "MON", "start": "10:00", "end": "09:00"})
    assert app.remove_weekly(added["entry"]["id"])["removed"] and app.list_weekly() == {"weekly": []}
