"""GraphFlow 라우팅 통합 테스트.

ReplayChatCompletionClient가 미리 정한 LLM 응답(도구 호출 포함)을 순서대로 재생하므로
API 키 없이 Agent 간 전달, 반려 루프, 종료 조건을 검증할 수 있다.
"""
import asyncio
import json

from autogen_agentchat.messages import BaseChatMessage
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient
from conftest import BAD_PLAN, GOOD_PLAN, TASKS_JSON

from planner_system.workflow import build_flow

_n = 0


def call(name: str, **kwargs) -> CreateResult:
    global _n
    _n += 1
    return CreateResult(
        finish_reason="function_calls",
        content=[FunctionCall(id=f"call_{_n}", name=name, arguments=json.dumps(kwargs, ensure_ascii=False))],
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0),
        cached=False,
    )


def run(ctx, responses):
    client = ReplayChatCompletionClient(
        responses,
        model_info={"vision": False, "function_calling": True, "json_output": True,
                    "family": "unknown", "structured_output": True},
    )
    flow = build_flow(ctx, client)
    result = asyncio.run(flow.run(task="과제 계획을 세워 주세요."))
    # 도구 호출 이벤트를 제외한 Agent 발화(chat message)만 추려 실행 순서를 확인한다.
    return result, [m.source for m in result.messages if isinstance(m, BaseChatMessage)]


def test_revise_loop_then_approve(ctx):
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "작업 2개를 등록했습니다.",
        call("get_free_slots"), call("submit_plan", plan_json=BAD_PLAN),
        call("validate_plan"), "REVISE\n화요수업과 겹칩니다.",
        call("submit_plan", plan_json=GOOD_PLAN), "수정 계획 제출",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "일정 요약...\nPLAN_COMPLETE",
    ]
    result, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "validator", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and ctx.attempts == 2
    assert "PLAN_COMPLETE" in result.messages[-1].content
    assert any(f.suffix == ".ics" for f in ctx.output_files)


def test_router_ignores_false_approve(ctx):
    """Validator LLM이 도구를 호출하지 않고 멋대로 APPROVE해도, 라우터가 직접 검증해 반려한다."""
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록 완료",
        call("get_free_slots"), call("submit_plan", plan_json=BAD_PLAN),
        "APPROVE (도구 호출 없이 승인)",
        call("submit_plan", plan_json=GOOD_PLAN), "재제출",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources.count("planner") == 2  # 거짓 승인 후 planner로 반려됨
    assert ctx.status == "approved"


def test_missing_assignment_goes_to_failure_report(ctx, tmp_path):
    ctx.assignment_path = tmp_path / "missing.pdf"
    responses = [
        call("read_assignment"), "과제 파일을 읽을 수 없습니다.",
        call("export_plan"), "파일이 없어 실패했습니다.\nPLAN_FAILED",
    ]
    result, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "exporter"]
    assert ctx.status == "failed" and "PLAN_FAILED" in result.messages[-1].content
    assert ctx.output_files[0].name.startswith("failure_report_")


def test_max_attempts_stops_loop(ctx):
    responses = [call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록"]
    for _ in range(3):
        responses += [call("submit_plan", plan_json=BAD_PLAN), "제출", call("validate_plan"), "REVISE"]
    responses += [call("export_plan"), "PLAN_FAILED"]
    _, sources = run(ctx, responses)
    assert sources.count("validator") == 3 and sources[-1] == "exporter"
    assert ctx.status == "failed" and "최대 시도" in ctx.failure_reason


def test_planner_text_plan_is_captured(ctx):
    """Planner가 수정 계획을 도구 호출 없이 텍스트(JSON)로만 보내도 시스템이 대신 제출해 검증을 이어간다."""
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=BAD_PLAN),
        call("validate_plan"), "REVISE",
        json.dumps({"plan_json": GOOD_PLAN}),  # 도구 호출 대신 텍스트로 전송
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "validator", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and ctx.attempts == 2


def test_task_breakdown_problem_routes_back_to_analyzer(ctx):
    """검증에서 작업 단계 문제가 나오면 Validator → Analyzer로 돌아가 다시 나눈 뒤 Planner가 새로 계획한다."""
    from test_validation import BIG_TASK, BIG_TASK_PLAN

    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=BIG_TASK), "작업 1개 등록",
        call("get_free_slots"), call("submit_plan", plan_json=BIG_TASK_PLAN),
        call("validate_plan"), "REANALYZE\nT1이 10시간입니다. 구현과 리포트로 나누세요.",
        call("submit_tasks", tasks_json=TASKS_JSON), "작업을 2개로 다시 나눴습니다.",
        call("get_free_slots"), call("submit_plan", plan_json=GOOD_PLAN),
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "validator", "analyzer", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and ctx.reanalysis_count == 1 and ctx.attempts == 1
    assert list(ctx.tasks) == ["T1", "T2"]


def test_validator_llm_can_request_reanalysis_via_tool(ctx):
    """규칙은 통과했지만 Validator가 빠진 제출물을 발견하면 request_reanalysis 도구로 Analyzer에 반려한다."""
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=GOOD_PLAN),
        call("validate_plan"), call("request_reanalysis", reason="발표 자료 작업이 빠져 있음"), "REANALYZE\n발표 자료 작업을 추가하세요.",
        call("submit_tasks", tasks_json=TASKS_JSON), "다시 등록",
        call("get_free_slots"), call("submit_plan", plan_json=GOOD_PLAN),
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "validator", "analyzer", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and ctx.reanalysis_reason == "발표 자료 작업이 빠져 있음"


def test_planner_postpones_existing_plan_then_replans(tmp_path):
    """시간이 부족하면 Planner가 기존 계획을 미루고, 라우터가 Planner를 한 번 더 실행해 계획을 제출하게 한다."""
    from datetime import datetime

    from conftest import START, TIMETABLE
    from test_rescheduling import URGENT_PLAN, URGENT_TASKS, other_sessions

    from planner_system.context import PlanContext
    from planner_system.plan_store import PlanStore

    store = PlanStore(tmp_path / "plans.json")
    store.upsert("other.pdf", other_sessions(), datetime(2026, 10, 5, 23, 59))
    store.save()
    urgent = tmp_path / "urgent.txt"
    urgent.write_text("긴급 과제: 문제 풀이와 제출. 마감 2026-09-29 23:59", encoding="utf-8")
    ctx = PlanContext(urgent, TIMETABLE, START, output_dir=tmp_path / "out", verbose=False,
                      plan_store=PlanStore(tmp_path / "plans.json"))
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=URGENT_TASKS), "등록",
        call("get_free_slots"), call("postpone_existing_plans"),
        call("submit_plan", plan_json=URGENT_PLAN), "제출",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    _, sources = run(ctx, responses)
    assert sources == ["user", "analyzer", "planner", "planner", "validator", "exporter"]
    assert ctx.status == "approved" and len(ctx.postponed_log) == 2
    assert PlanStore(tmp_path / "plans.json").find("other.pdf")["sessions"][0]["date"] == "2026-10-01"


def test_false_approve_is_replaced_by_real_verdict(ctx):
    """Validator가 도구 없이 APPROVE라고 답해도, 다른 Agent에게는 실제 검증 결과(반려 사유)가 전달된다."""
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=BAD_PLAN),
        "APPROVE\n계획이 좋습니다.",  # 도구 호출 없이 승인 → 시스템이 검증 결과로 바꾼다
        call("submit_plan", plan_json=GOOD_PLAN), "제출했습니다.",
        call("validate_plan"), "APPROVE",
        call("export_plan"), "PLAN_COMPLETE",
    ]
    result, _ = run(ctx, responses)
    first = next(m for m in result.messages if isinstance(m, BaseChatMessage) and m.source == "validator")
    assert first.content.startswith("REVISE") and "시스템이 직접 검증한 결과" in first.content
    assert "'화요수업'(10:00-12:00) 일정과 겹칩니다" in first.content  # Planner가 고칠 내용을 받는다
    assert ctx.status == "approved"


def test_exporter_skipping_tool_still_exports(ctx):
    responses = [
        call("read_assignment"), call("submit_tasks", tasks_json=TASKS_JSON), "등록",
        call("get_free_slots"), call("submit_plan", plan_json=GOOD_PLAN),
        call("validate_plan"), "APPROVE",
        "STOP",  # Exporter가 도구를 호출하지 않음
    ]
    result, sources = run(ctx, responses)
    assert sources[-1] == "exporter" and "PLAN_COMPLETE" in result.messages[-1].content
    assert any(f.suffix == ".ics" for f in ctx.output_files)
