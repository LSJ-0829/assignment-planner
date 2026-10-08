"""식사 시간 확보, 비울 시간대 확정(reserve_meal), 작업을 끊지 않는 규칙 테스트."""
import json
from datetime import date, datetime

import pytest
from test_workflow_offline import call, run

from planner_system import tools
from planner_system.context import PlanContext
from planner_system.schedule import Meal
from planner_system.validation import meal_candidates

# 기본 식사 설정 사용: 점심 11:30-14:00, 저녁 17:30-20:30 사이에 60분. 목요일 12:00-13:15 수업.
TIMETABLE = {
    "classes": [{"name": "목요수업", "day": "THU", "start": "12:00", "end": "13:15"}],
    "events": [],
    "preferences": {"day_start": "09:00", "day_end": "24:00", "min_session_minutes": 60},
}
START = datetime(2026, 9, 28, 18, 0)  # 월요일. 10/1(목), 10/3(토)
SIX_HOURS = json.dumps([{"id": "T1", "title": "구현", "hours": 6, "deadline": "2026-10-04 23:59"}])
THROUGH_LUNCH = json.dumps([{"task_id": "T1", "date": "2026-10-03", "start": "10:00", "end": "16:00"}])
AROUND_LUNCH = json.dumps([
    {"task_id": "T1", "date": "2026-10-03", "start": "10:00", "end": "12:30"},
    {"task_id": "T1", "date": "2026-10-03", "start": "13:30", "end": "17:00"},
])
LUNCH = Meal("점심", 11 * 60 + 30, 14 * 60, 60, 12 * 60)
THREE_HOURS = json.dumps([{"id": "T1", "title": "구현", "hours": 3, "deadline": "2026-10-04 23:59"}])


@pytest.fixture
def ctx(tmp_path):
    p = tmp_path / "assignment.txt"
    p.write_text("과제: 기능 구현. 마감 2026-10-04 23:59까지 제출", encoding="utf-8")
    return PlanContext(p, TIMETABLE, START, output_dir=tmp_path / "out", verbose=False, max_reanalysis=0)


def test_default_meals_are_already_removed_from_free_time(ctx):
    """기본 식사 시간(점심 12:00, 저녁 18:00)은 미리 비워 두어, Planner가 식사를 따로 계산하지 않아도 된다."""
    tools.submit_tasks(ctx, SIX_HOURS)
    text = tools.get_free_slots(ctx)
    assert "점심·저녁 식사 시간은 빈 시간에서 이미 제외됨" in text
    assert "2026-10-03(토): 09:00-12:00, 13:00-18:00, 19:00-24:00" in text
    # 목요일은 수업(12:00-13:15)이 점심 시간대를 막아 점심은 미리 정하지 않는다 → 검증 단계에서 판단
    assert [name for _, _, name in ctx.schedule.reserved[date(2026, 10, 1)]] == ["저녁 식사"]


def test_working_through_lunch_conflicts_with_reserved_meal(ctx):
    tools.submit_tasks(ctx, SIX_HOURS)
    tools.submit_plan(ctx, THROUGH_LUNCH)
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: FAIL") and "'점심 식사'(12:00-13:00) 일정과 겹칩니다" in out

    tools.submit_plan(ctx, json.dumps([  # 식사 전후로 이어서 배치 → 떨어진 것으로 보지 않는다
        {"task_id": "T1", "date": "2026-10-03", "start": "10:00", "end": "12:00"},
        {"task_id": "T1", "date": "2026-10-03", "start": "13:00", "end": "17:00"},
    ]))
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: PASS") and "[식사 시간] 문제 없음" in out and "떨어져" not in out


def test_validator_can_move_a_meal_and_plan_must_avoid_it(ctx):
    tools.submit_tasks(ctx, SIX_HOURS)
    out = tools.reserve_meal(ctx, "2026-10-03", "점심", "12:30")  # 기본 12:00 대신 12:30으로 옮김
    assert "식사 시간 확정: 2026-10-03(토) 점심 식사 12:30-13:30" in out
    assert [r for r in ctx.schedule.reserved[date(2026, 10, 3)] if r[2] == "점심 식사"] == [(750, 810, "점심 식사")]
    tools.submit_plan(ctx, AROUND_LUNCH)
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")
    tools.export_plan(ctx)
    md = next(f for f in ctx.output_files if f.suffix == ".md").read_text(encoding="utf-8")
    assert "검증 단계에서 확정한 식사 시간" in md and "점심 식사 12:30-13:30" in md


def test_reserve_meal_rejects_class_time_and_bad_input(ctx):
    tools.submit_tasks(ctx, SIX_HOURS)
    before = {d: list(v) for d, v in ctx.schedule.reserved.items()}
    out = tools.reserve_meal(ctx, "2026-10-01", "점심", "12:00")  # 목요수업 12:00-13:15
    assert out.startswith("오류") and "'목요수업'" in out and "가능한 시작 시각" in out
    assert tools.reserve_meal(ctx, "2026-10-01", "야식", "12:00").startswith("오류")
    assert tools.reserve_meal(ctx, "10월 1일", "점심", "12:00").startswith("오류")
    assert ctx.schedule.reserved == before and ctx.meal_log == []


def test_candidates_when_class_blocks_the_meal_window():
    """수업이 식사 시간대를 막으면 수업 직전(이른 식사)과 직후(늦은 식사)를 후보로 낸다."""
    blocked = [(12 * 60, 13 * 60 + 15)]  # 12:00-13:15 수업 → 시간대 안에 60분 자리가 없다
    assert meal_candidates(LUNCH, blocked) == [(11 * 60, " (이른 식사)"), (13 * 60 + 15, " (늦은 식사)")]
    assert meal_candidates(LUNCH, [])[0] == (12 * 60, "")  # 막힌 게 없으면 선호 시각
    assert meal_candidates(LUNCH, [(7 * 60, 24 * 60)]) == []  # 하루 종일 막혀 있으면 후보 없음


def test_unnecessary_split_is_rejected_once_then_only_warned(ctx):
    tools.submit_tasks(ctx, json.dumps([{"id": "T1", "title": "구현", "hours": 3, "deadline": "2026-10-04 23:59"}]))
    split = json.dumps([
        {"task_id": "T1", "date": "2026-10-02", "start": "10:00", "end": "11:00"},
        {"task_id": "T1", "date": "2026-10-03", "start": "15:00", "end": "17:00"},
    ])
    tools.submit_plan(ctx, split)
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: FAIL") and "2번으로 떨어져 있습니다" in out and "같은 날에 이어서 배치" in out

    tools.submit_plan(ctx, split)  # 다시 같은 계획: '가능하면'이므로 두 번째부터는 경고로만 남긴다
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: PASS") and "떨어져 있습니다" in out


BOTH_SIDES_OF_CLASS = json.dumps([  # 목요수업(12:00-13:15) 앞뒤를 모두 과제로 채운 계획
    {"task_id": "T1", "date": "2026-10-01", "start": "10:30", "end": "12:00"},
    {"task_id": "T1", "date": "2026-10-01", "start": "13:15", "end": "14:45"},
])


def test_workflow_validator_decides_meal_slot(ctx):
    """수업 때문에 점심이 밀리는 날, Validator가 이른/늦은 식사 중 비울 쪽을 판단해 확정하고 Planner가 다시 배치한다."""
    late_lunch = json.dumps([
        {"task_id": "T1", "date": "2026-10-01", "start": "10:30", "end": "12:00"},
        {"task_id": "T1", "date": "2026-10-01", "start": "14:15", "end": "15:45"},
    ])
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=THREE_HOURS), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=BOTH_SIDES_OF_CLASS),
        call("validate_plan"), call("reserve_meal", date="2026-10-01", meal="점심", start="13:15"),
        "REVISE\n10/1 13:15-14:15은 점심 시간으로 비우세요.",
        call("submit_plan", plan_json=late_lunch), "수정 제출",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "validator", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and ctx.meal_log == ["2026-10-01(목) 점심 식사 13:15-14:15"]


def test_workflow_system_picks_meal_slot_if_validator_does_not(ctx):
    """Validator가 시간대를 정하지 않으면 라우터가 첫 후보(이른 식사 11:00)로 확정한다."""
    early_lunch = json.dumps([
        {"task_id": "T1", "date": "2026-10-01", "start": "09:30", "end": "11:00"},
        {"task_id": "T1", "date": "2026-10-01", "start": "13:15", "end": "14:45"},
    ])
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=THREE_HOURS), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=BOTH_SIDES_OF_CLASS),
        call("validate_plan"), "REVISE\n점심 시간을 비우세요.",
        call("submit_plan", plan_json=early_lunch), "수정 제출",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    run(ctx, responses)
    assert ctx.status == "approved" and ctx.meal_log == ["2026-10-01(목) 점심 식사 11:00-12:00"]


def test_class_in_meal_window_is_fine_if_a_slot_next_to_it_is_free(ctx):
    """목요수업(12:00-13:15)이 점심 시간대를 막아도, 수업 직전이나 직후가 비어 있으면 문제가 아니다."""
    tools.submit_tasks(ctx, THREE_HOURS)
    tools.submit_plan(ctx, json.dumps([{"task_id": "T1", "date": "2026-10-01", "start": "13:15", "end": "16:15"}]))
    out = tools.validate_plan(ctx)  # 수업 직후는 과제가 차지했지만 수업 직전(11:00-12:00)이 비어 있다
    assert out.startswith("검증 결과: PASS") and "[식사 시간] 문제 없음" in out


def test_validator_must_choose_when_plan_takes_both_slots_around_class(ctx):
    """수업 앞뒤 자리를 계획이 모두 차지하면, 이른 식사와 늦은 식사 중 어느 쪽을 비울지 판단해야 한다."""
    tools.submit_tasks(ctx, THREE_HOURS)
    tools.submit_plan(ctx, BOTH_SIDES_OF_CLASS)
    out = tools.validate_plan(ctx)
    assert "[식사 시간] 확보되지 않은 식사 1건" in out and "reserve_meal 도구로 확정" in out
    assert "2026-10-01(목) 점심" in out
    assert "11:00-12:00 (이른 식사), 13:15-14:15 (늦은 식사)" in out
    assert "확정" in tools.reserve_meal(ctx, "2026-10-01", "점심", "13:15")  # 늦은 식사를 선택
    assert ctx.meal_log == ["2026-10-01(목) 점심 식사 13:15-14:15"]


def test_past_meal_window_today_is_ignored(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("과제: 기능 구현. 마감 2026-10-04 23:59까지 제출", encoding="utf-8")
    late = PlanContext(p, TIMETABLE, datetime(2026, 9, 28, 22, 0), output_dir=tmp_path, verbose=False, max_reanalysis=0)
    tools.submit_tasks(late, json.dumps([{"id": "T1", "title": "구현", "hours": 1.5, "deadline": "2026-10-04 23:59"}]))
    tools.submit_plan(late, json.dumps([{"task_id": "T1", "date": "2026-09-28", "start": "22:00", "end": "23:30"}]))
    out = tools.validate_plan(late)  # 밤 10시에 시작: 오늘 점심·저녁 시간대는 이미 지났다
    assert out.startswith("검증 결과: PASS") and "[식사 시간] 문제 없음" in out


# ----- 하루 권장 시간 (몰아서 배치하지 않기) -----------------------------------------
TWO_TASKS = json.dumps([
    {"id": "T1", "title": "구현", "hours": 5, "deadline": "2026-10-04 23:59"},
    {"id": "T2", "title": "보고서", "hours": 5, "deadline": "2026-10-04 23:59", "depends_on": ["T1"]},
])


SPREAD = {**TIMETABLE, "preferences": {**TIMETABLE["preferences"], "recommended_daily_hours": 6}}  # 선택 기능을 켠 시간표


def test_default_is_to_finish_as_early_as_possible(ctx):
    """기본 방침: 하루 제한 없이 가장 이른 시간부터 채운다. (식사 시간만 비우면 하루 10시간도 통과)"""
    tools.submit_tasks(ctx, TWO_TASKS)
    assert "[하루 배치 한도]" not in tools.get_free_slots(ctx)
    tools.submit_plan(ctx, json.dumps([
        {"task_id": "T1", "date": "2026-09-29", "start": "09:00", "end": "12:00"},
        {"task_id": "T1", "date": "2026-09-29", "start": "13:00", "end": "15:00"},
        {"task_id": "T2", "date": "2026-09-29", "start": "15:00", "end": "18:00"},
        {"task_id": "T2", "date": "2026-09-29", "start": "19:00", "end": "21:00"},
    ]))
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")


def test_cramming_into_one_day_is_rejected_when_spreading_is_enabled(ctx):
    ctx.schedule.recommended_daily_hours = 6
    tools.submit_tasks(ctx, TWO_TASKS)
    assert "[하루 배치 한도] 하루에 과제를 6h까지만 배치" in tools.get_free_slots(ctx)
    tools.submit_plan(ctx, json.dumps([  # 토요일 하루에 10시간 (식사 시간은 비움)
        {"task_id": "T1", "date": "2026-10-03", "start": "09:00", "end": "12:00"},
        {"task_id": "T1", "date": "2026-10-03", "start": "13:00", "end": "15:00"},
        {"task_id": "T2", "date": "2026-10-03", "start": "15:00", "end": "18:00"},
        {"task_id": "T2", "date": "2026-10-03", "start": "19:00", "end": "21:00"},
    ]))
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: FAIL") and "2026-10-03(토) 총 10.0h 배치: 하루 권장 6h를 넘습니다" in out

    tools.submit_plan(ctx, json.dumps([  # 이틀로 나눔
        {"task_id": "T1", "date": "2026-10-02", "start": "13:00", "end": "18:00"},
        {"task_id": "T2", "date": "2026-10-03", "start": "13:00", "end": "18:00"},
    ]))
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")


def test_daily_limit_relaxes_automatically_for_urgent_work(tmp_path):
    """마감이 촉박해 하루 6시간으로는 불가능하면, 가능한 최소 시간까지 한도를 늘린다."""
    p = tmp_path / "a.txt"
    p.write_text("과제: 기능 구현. 마감 2026-09-29 23:59까지 제출", encoding="utf-8")
    urgent = PlanContext(p, SPREAD, datetime(2026, 9, 29, 9, 0), output_dir=tmp_path, verbose=False, max_reanalysis=0)
    tools.submit_tasks(urgent, json.dumps([
        {"id": "T1", "title": "구현", "hours": 5, "deadline": "2026-09-29 23:59"},
        {"id": "T2", "title": "보고서", "hours": 4, "deadline": "2026-09-29 23:59", "depends_on": ["T1"]},
    ]))
    assert urgent.schedule.daily_limit(list(urgent.tasks.values())) == 9
    assert "9h까지만 배치" in tools.get_free_slots(urgent) and "늘렸습니다" in tools.get_free_slots(urgent)
    tools.submit_plan(urgent, json.dumps([
        {"task_id": "T1", "date": "2026-09-29", "start": "09:00", "end": "12:00"},
        {"task_id": "T1", "date": "2026-09-29", "start": "13:00", "end": "15:00"},
        {"task_id": "T2", "date": "2026-09-29", "start": "15:00", "end": "18:00"},
        {"task_id": "T2", "date": "2026-09-29", "start": "19:00", "end": "20:00"},
    ]))
    assert tools.validate_plan(urgent).startswith("검증 결과: PASS")  # 하루 9시간이지만 급한 과제라 허용
