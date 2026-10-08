"""작업 단계 검증과 재분해(Analyzer 반려) 판정 테스트."""
import json

from conftest import GOOD_PLAN, TASKS_JSON

from planner_system import tools
from planner_system.schedule import Task
from planner_system.validation import find_dependency_cycle, review_tasks

BIG_TASK = json.dumps([{"id": "T1", "title": "전체 구현", "hours": 10, "deadline": "2026-10-05 23:59"}])
BIG_TASK_PLAN = json.dumps([
    {"task_id": "T1", "date": "2026-10-01", "start": "09:00", "end": "13:00"},
    {"task_id": "T1", "date": "2026-10-02", "start": "09:00", "end": "13:00"},
    {"task_id": "T1", "date": "2026-10-03", "start": "09:00", "end": "11:00"},
])


def test_dependency_cycle_is_rejected_at_submit(ctx):
    cyclic = json.dumps([
        {"id": "T1", "title": "a", "hours": 1, "deadline": "2026-10-02", "depends_on": ["T2"]},
        {"id": "T2", "title": "b", "hours": 1, "deadline": "2026-10-02", "depends_on": ["T1"]},
    ])
    assert "선행 관계가 순환" in tools.submit_tasks(ctx, cyclic)
    assert not ctx.tasks


def test_review_tasks_flags_oversized_and_unsplit_work(ctx):
    tools.submit_tasks(ctx, BIG_TASK)
    issues = review_tasks(ctx.tasks)
    assert any("8시간을 넘습니다" in x for x in issues)
    assert any("작업이 1개뿐" in x for x in issues)
    tools.submit_tasks(ctx, TASKS_JSON)
    assert review_tasks(ctx.tasks) == []
    assert find_dependency_cycle(ctx.tasks) is None


def test_duplicate_titles_flagged():
    from datetime import datetime

    dl = datetime(2026, 10, 2)
    tasks = {"T1": Task("T1", "구현", 2, dl), "T2": Task("T2", "구현", 2, dl)}
    assert any("같은 이름의 작업" in x for x in review_tasks(tasks))


def test_task_problem_sends_back_to_analyzer_then_passes(ctx):
    """일정은 규칙을 지켜도 작업 단계가 잘못됐으면 Analyzer로 반려하고, 다시 나누면 통과한다."""
    tools.submit_tasks(ctx, BIG_TASK)
    tools.submit_plan(ctx, BIG_TASK_PLAN)
    out = tools.validate_plan(ctx)
    assert "작업 단계 재분해 필요" in out and "[작업 단계 검증] 문제 2건" in out
    assert ctx.status == "reanalyze" and ctx.reanalysis_count == 1
    assert ctx.attempts == 0  # 재분해는 일정 검증 횟수에 넣지 않는다

    assert "재등록 완료" in tools.submit_tasks(ctx, TASKS_JSON)  # Analyzer가 다시 나눠 제출
    assert ctx.sessions == [] and not ctx.plan_pending  # 이전 계획은 무효
    tools.submit_plan(ctx, GOOD_PLAN)
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: PASS") and "[작업 단계 검증] 문제 없음" in out
    assert ctx.status == "approved" and ctx.attempts == 1


def test_reanalysis_limit_downgrades_to_warning(ctx):
    ctx.max_reanalysis = 0
    tools.submit_tasks(ctx, BIG_TASK)
    tools.submit_plan(ctx, BIG_TASK_PLAN)
    out = tools.validate_plan(ctx)
    assert out.startswith("검증 결과: PASS") and "(작업 단계)" in out
    assert ctx.status == "approved"


def test_infeasible_takes_priority_over_reanalysis(ctx):
    """시간이 절대적으로 부족하면 작업을 다시 나눠도 소용없으므로, 재분해 대신 바로 비상 계획으로 넘어간다."""
    tools.submit_tasks(ctx, json.dumps([{"id": "T1", "title": "대형 과제", "hours": 12, "deadline": "2026-09-29 23:59"}]))
    assert tools.validate_plan(ctx).startswith("검증 결과: EMERGENCY")
    assert ctx.status == "approved" and ctx.reanalysis_count == 0 and ctx.emergency


def test_validator_can_request_reanalysis_once(ctx):
    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, GOOD_PLAN)
    out = tools.validate_plan(ctx)
    assert ctx.status == "approved"
    assert "[작업 단계 대조용] 과제 원문 발췌" in out  # 처음 한 번만 원문 발췌 제공

    assert "접수" in tools.request_reanalysis(ctx, "시연 영상 작업이 빠져 있음")
    assert ctx.status == "reanalyze" and ctx.reanalysis_reason == "시연 영상 작업이 빠져 있음"

    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, GOOD_PLAN)
    out = tools.validate_plan(ctx)
    assert "[작업 단계 대조용]" not in out and ctx.status == "approved"
    assert "한도" in tools.request_reanalysis(ctx, "또 요청")  # 한도 초과 → 반영하지 않음
    assert ctx.status == "approved"
