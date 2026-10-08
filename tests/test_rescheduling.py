"""긴박한 과제를 위해 기존 계획을 미루는 기능 테스트.

시나리오(conftest 시간표, 2026-09-28(월) 18:00 시작, 하루 상한 4h):
  - 저장된 과제 other.pdf (마감 10/05): 9/28 20:00-23:00, 9/29 13:00-17:00
  - 새 과제(마감 9/29 23:59, 총 8h) → 기존 계획이 자리를 차지해 3h 부족
"""
import json
from datetime import datetime

import pytest
from conftest import START, TIMETABLE

from planner_system import rescheduling, tools
from planner_system.context import PlanContext
from planner_system.plan_store import PlanStore

URGENT_TASKS = json.dumps([
    {"id": "T1", "title": "문제 풀이", "hours": 4, "deadline": "2026-09-29 23:59"},
    {"id": "T2", "title": "정리 및 제출", "hours": 4, "deadline": "2026-09-29 23:59", "depends_on": ["T1"]},
])
URGENT_PLAN = json.dumps([
    {"task_id": "T1", "date": "2026-09-28", "start": "18:00", "end": "19:00"},
    {"task_id": "T1", "date": "2026-09-28", "start": "20:00", "end": "23:00"},
    {"task_id": "T2", "date": "2026-09-29", "start": "13:00", "end": "17:00"},
])


def other_sessions(deadline="2026-10-05 23:59"):
    return [
        {"task_id": "T1", "title": "자료 조사", "date": "2026-09-28", "start": "20:00", "end": "23:00", "deadline": deadline},
        {"task_id": "T2", "title": "보고서 작성", "date": "2026-09-29", "start": "13:00", "end": "17:00", "deadline": deadline},
    ]


@pytest.fixture
def make(tmp_path):
    def _make(other_deadline="2026-10-05 23:59", other_priority="normal", priority="normal"):
        store = PlanStore(tmp_path / "plans.json")
        store.upsert("other.pdf", other_sessions(other_deadline), datetime.strptime(other_deadline, "%Y-%m-%d %H:%M"),
                     other_priority)
        store.save()
        urgent = tmp_path / "urgent.txt"
        urgent.write_text("긴급 과제: 문제 풀이와 제출. 마감 2026-09-29 23:59", encoding="utf-8")
        ctx = PlanContext(urgent, TIMETABLE, START, output_dir=tmp_path / "out", verbose=False,
                          plan_store=PlanStore(tmp_path / "plans.json"), priority=priority)
        tools.submit_tasks(ctx, URGENT_TASKS)
        return ctx

    return _make


def test_shortage_is_detected_and_tool_is_suggested(make):
    ctx = make()
    _, short = rescheduling.current_shortage(ctx)
    assert short == pytest.approx(3.0)
    text = tools.get_free_slots(ctx)
    assert "부족 3.0h" in text and "postpone_existing_plans 도구를 호출" in text
    assert ctx.postponed == {}  # 가능 여부를 미리 계산해도 상태는 바뀌지 않는다


def test_postpone_frees_time_and_keeps_other_deadline_and_order(make):
    ctx = make()
    out = tools.postpone_existing_plans(ctx)
    assert "2개 세션을 뒤로 미뤘습니다" in out and "시간 부족이 해소" in out
    assert "→ OK" in out  # 갱신된 빈 시간표가 함께 반환된다
    assert rescheduling.current_shortage(ctx) is None and ctx.just_postponed

    plan_id = ctx.plan_store.find("other.pdf")["id"]
    moved = ctx.postponed[plan_id]
    starts = [datetime.fromisoformat(f"{s['date']} {s['start']}") for s in moved]
    assert all(t >= datetime(2026, 9, 29, 23, 59) for t in starts)  # 긴박한 마감 뒤로 이동
    assert all(t < datetime(2026, 10, 5, 23, 59) for t in starts)  # 자기 마감은 지킴
    assert [s["title"] for s in moved] == ["자료 조사", "보고서 작성"] and starts == sorted(starts)  # 순서 유지
    assert [s["end"] for s in moved] == ["12:00", "16:00"]  # 길이(3h, 4h) 유지


def test_changes_are_saved_only_when_plan_is_approved(make, tmp_path):
    ctx = make()
    tools.postpone_existing_plans(ctx)
    # 아직 저장소 파일은 그대로
    assert PlanStore(tmp_path / "plans.json").find("other.pdf")["sessions"][0]["date"] == "2026-09-28"

    tools.submit_plan(ctx, URGENT_PLAN)
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")
    out = tools.export_plan(ctx)
    assert "미룬 기존 계획: 2개 세션" in out

    saved = PlanStore(tmp_path / "plans.json")
    other = saved.find("other.pdf")
    assert [s["date"] for s in other["sessions"]] == ["2026-10-01", "2026-10-01"] and other.get("rescheduled")
    assert saved.find("urgent.txt")["priority"] == "normal"
    assert "미룬 기존 일정" in next(f for f in ctx.output_files if f.suffix == ".md").read_text(encoding="utf-8")


def test_higher_priority_plan_is_not_postponed(make):
    ctx = make(other_priority="high")
    assert "미룰 수 있는 기존 계획이 없습니다" in tools.postpone_existing_plans(ctx)
    assert ctx.postponed == {} and rescheduling.current_shortage(ctx) is not None
    assert "postpone_existing_plans" not in tools.get_free_slots(ctx)


def test_high_priority_assignment_can_postpone_normal_plan(make):
    ctx = make(priority="high")
    assert "뒤로 미뤘습니다" in tools.postpone_existing_plans(ctx)


def test_plan_with_tight_deadline_is_not_postponed(make):
    ctx = make(other_deadline="2026-09-29 23:59")  # 미루면 그 과제의 마감을 넘긴다
    assert "미룰 수 있는 기존 계획이 없습니다" in tools.postpone_existing_plans(ctx)
    assert ctx.postponed == {}


def test_validator_asks_for_postponing_instead_of_failing(make):
    ctx = make()
    tools.submit_plan(ctx, URGENT_PLAN)  # 미루지 않고 제출 → 기존 계획과 겹침
    out = tools.validate_plan(ctx)
    assert "기존 계획 미루기 필요" in out and ctx.status == "planning"


def test_no_postponing_when_time_is_enough(make):
    ctx = make()
    tools.submit_tasks(ctx, json.dumps([{"id": "T1", "title": "짧은 과제", "hours": 2, "deadline": "2026-10-02 23:59"}]))
    assert "미룰 필요가 없습니다" in tools.postpone_existing_plans(ctx)
    assert ctx.postponed == {}
