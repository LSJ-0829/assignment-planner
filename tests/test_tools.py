"""Tool 단위 테스트 (API 키 없이 실행 가능)."""
import json
from datetime import date

from conftest import BAD_PLAN, GOOD_PLAN, TASKS_JSON

from planner_system import tools


def test_read_assignment(ctx):
    text = tools.read_assignment(ctx)
    assert "코드 구현" in text and "[계획 시작 시점] 2026-09-28(월) 18:00" in text


def test_read_assignment_missing_file(ctx, tmp_path):
    ctx.assignment_path = tmp_path / "nope.pdf"
    assert tools.read_assignment(ctx).startswith("오류")
    assert "찾을 수 없음" in ctx.failure_reason


def test_submit_tasks_ok_and_json_in_codeblock(ctx):
    out = tools.submit_tasks(ctx, f"```json\n{TASKS_JSON}\n```")
    assert "작업 2개 등록 완료" in out and ctx.status == "planning"


def test_submit_tasks_rejects_bad_input(ctx):
    assert tools.submit_tasks(ctx, "[]").startswith("오류")
    bad_dep = json.dumps([{"id": "T1", "title": "a", "hours": 1, "deadline": "2026-10-01", "depends_on": ["T9"]}])
    assert "존재하지 않는 선행 작업" in tools.submit_tasks(ctx, bad_dep)
    past = json.dumps([{"id": "T1", "title": "a", "hours": 1, "deadline": "2026-09-01"}])
    assert "이전" in tools.submit_tasks(ctx, past)
    assert not ctx.tasks


def test_free_slots_excludes_classes_and_past(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    # 월요일: 18:00 시작, 19:00-20:00 수업 → 18:00-19:00, 20:00-23:00
    assert ctx.schedule.free_slots(date(2026, 9, 28)) == [(18 * 60, 19 * 60), (20 * 60, 23 * 60)]
    # 수요일: 종일 약속
    assert ctx.schedule.free_slots(date(2026, 9, 30)) == []
    text = tools.get_free_slots(ctx)
    assert "2026-09-30(수): 없음" in text and "→ OK" in text


def test_validate_pass(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, GOOD_PLAN)
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: PASS") and ctx.status == "approved"


def test_validate_detects_conflict_and_shortage(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, BAD_PLAN)
    out = tools.validate_plan(ctx)
    assert "FAIL" in out and ctx.status == "planning"
    assert "'화요수업'(10:00-12:00) 일정과 겹칩니다" in out
    assert "T1(코드 구현) 배정 2.0h < 필요 3.0h" in out


def test_validate_detects_daily_cap_deadline_and_order(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    plan = json.dumps([
        {"task_id": "T2", "date": "2026-09-29", "start": "13:00", "end": "15:00"},  # 선행 T1보다 먼저
        {"task_id": "T1", "date": "2026-09-29", "start": "15:00", "end": "18:00"},  # 하루 5h > 4h
        {"task_id": "T1", "date": "2026-10-03", "start": "10:00", "end": "11:00"},  # 마감 이후
    ])
    tools.submit_plan(ctx, plan)
    out = tools.validate_plan(ctx)
    assert "하루 최대 4.0h를 초과" in out
    assert "마감(2026-10-02 23:59) 이후" in out
    assert "순서 위반" in out


def test_validate_without_new_plan_fails(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    assert "새로 제출된 계획이 없습니다" in tools.validate_plan(ctx)


def test_max_attempts_leads_to_failed(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    for _ in range(3):
        tools.submit_plan(ctx, BAD_PLAN)
        out = tools.validate_plan(ctx)
    assert "재시도 한도 초과" in out and ctx.status == "failed"


def test_infeasible_detected_and_suggestion(ctx):
    tasks = json.dumps([{"id": "T1", "title": "대형 과제", "hours": 12, "deadline": "2026-09-29 23:59"}])
    tools.submit_tasks(ctx, tasks)
    assert "부족" in tools.get_free_slots(ctx)
    tools.submit_plan(ctx, json.dumps([{"task_id": "T1", "date": "2026-09-29", "start": "13:00", "end": "17:00"}]))
    out = tools.validate_plan(ctx)
    assert "INFEASIBLE" in out and ctx.status == "failed"
    report = tools.export_plan(ctx)
    assert "하루 최대 공부 시간을 4.0h →" in report
    assert ctx.output_files[0].name.startswith("failure_report_")


def test_export_ics(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, GOOD_PLAN)
    tools.validate_plan(ctx)
    tools.export_plan(ctx)
    ics = next(f for f in ctx.output_files if f.suffix == ".ics").read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "DTSTART:20260929T040000Z" in ics  # KST 13:00 = UTC 04:00


def test_parse_user_deadline():
    from datetime import datetime

    from planner_system.utils import parse_user_deadline

    start = datetime(2026, 9, 28, 18, 0)
    assert parse_user_deadline("2026-10-01 18:00", start) == datetime(2026, 10, 1, 18, 0)
    assert parse_user_deadline("10/1", start) == datetime(2026, 10, 1, 23, 59)
    assert parse_user_deadline("10월 1일", start) == datetime(2026, 10, 1, 23, 59)
    assert parse_user_deadline("10-01 09:30", start) == datetime(2026, 10, 1, 9, 30)
    assert parse_user_deadline("1/5", start) == datetime(2027, 1, 5, 23, 59)  # 지난 날짜 → 다음 해


def test_contains_date():
    from planner_system.utils import contains_date

    assert contains_date("최종 마감 2026. 10. 11. 11:59 PM")
    assert contains_date("10월 1일까지 제출")
    assert contains_date("Due Oct 3")
    assert not contains_date("1. 2. For the differential volume in Figure 3.26")


def test_user_deadline_limits_tasks_and_appears_in_prompt(ctx):
    from datetime import datetime

    ctx.user_deadline = datetime(2026, 10, 1, 23, 59)
    assert "[사용자 입력 마감] 2026-10-01 23:59" in tools.read_assignment(ctx)
    # TASKS_JSON의 마감(10/2)이 사용자 마감(10/1)보다 늦으므로 거부되어야 한다.
    assert "사용자 입력 마감" in tools.submit_tasks(ctx, TASKS_JSON)
    assert not ctx.tasks


def test_image_pdf_requires_note(ctx, tmp_path):
    p = tmp_path / "image_hw.txt"
    p.write_text("1.\n\n2.\n", encoding="utf-8")
    ctx.assignment_path = p
    assert "사용자 설명(--note)이 필요" in tools.read_assignment(ctx)
    ctx.user_note = "전기자기학 과제, 서술형 2문제"
    out = tools.read_assignment(ctx)
    assert "[사용자 설명] 전기자기학 과제" in out and "[주의]" in out


def test_extract_json_ignores_trailing_garbage():
    from planner_system.utils import extract_json

    assert extract_json('[{"id": "T1"}]}') == [{"id": "T1"}]
    assert extract_json('계획입니다:\n```json\n[1, 2]\n```') == [1, 2]


def test_commute_buffers_and_no_daily_cap(assignment_file, tmp_path):
    from datetime import date, datetime

    from planner_system.context import PlanContext

    tt = {
        "classes": [{"name": "수업A", "day": "TUE", "start": "13:30", "end": "14:45"},
                    {"name": "수업B", "day": "TUE", "start": "16:30", "end": "17:45"}],
        "events": [],
        "preferences": {"day_start": "09:00", "day_end": "24:00",
                        "commute_before_minutes": 120, "commute_after_minutes": 60, "meals": [],
                        "recommended_daily_hours": None},  # 권장 시간도 끄면 하루 제한이 전혀 없다
    }
    c = PlanContext(assignment_file, tt, datetime(2026, 9, 28, 8, 0), output_dir=tmp_path, verbose=False,
                    max_reanalysis=0)  # 이 테스트는 일정 규칙만 본다
    # 화요일: 11:30-13:30 등교, 수업 사이 공강 14:45-16:30은 사용 가능, 17:45-18:45 귀가
    assert c.schedule.free_slots(date(2026, 9, 29)) == [(9 * 60, 11 * 60 + 30), (14 * 60 + 45, 16 * 60 + 30), (18 * 60 + 45, 24 * 60)]
    # 수업 없는 수요일은 이동 시간 없음, 자정까지 사용 가능
    assert c.schedule.free_slots(date(2026, 9, 30)) == [(9 * 60, 24 * 60)]
    tools.submit_tasks(c, '[{"id":"T1","title":"과제","hours":10,"deadline":"2026-09-30 23:59"}]')
    assert "하루 최대" not in tools.get_free_slots(c)
    tools.submit_plan(c, '[{"task_id":"T1","date":"2026-09-30","start":"10:00","end":"20:00"}]')  # 하루 10h도 허용
    assert tools.validate_plan(c).startswith("검증 결과: PASS")
    tools.submit_plan(c, '[{"task_id":"T1","date":"2026-09-29","start":"12:00","end":"22:00"}]')
    assert "'등교 준비·이동'" in tools.validate_plan(c)


def test_adjacent_sessions_of_same_task_are_merged(ctx):
    """같은 작업이 바로 이어지는 세션으로 쪼개져 제출되면 하나로 합친다. (화면에 같은 일정이 두 번 보이지 않게)"""
    tools.submit_tasks(ctx, TASKS_JSON)
    out = tools.submit_plan(ctx, json.dumps([
        {"task_id": "T1", "date": "2026-09-29", "start": "13:00", "end": "15:00"},
        {"task_id": "T1", "date": "2026-09-29", "start": "15:00", "end": "16:00"},  # 바로 이어짐 → 합침
        {"task_id": "T2", "date": "2026-10-01", "start": "10:00", "end": "11:00"},
        {"task_id": "T2", "date": "2026-10-01", "start": "11:30", "end": "12:30"},  # 30분 쉬었으므로 그대로
    ]))
    assert "세션 3개" in out
    assert [(s.task_id, s.start, s.end) for s in ctx.sessions] == [
        ("T1", 13 * 60, 16 * 60), ("T2", 10 * 60, 11 * 60), ("T2", 11 * 60 + 30, 12 * 60 + 30)]
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")
