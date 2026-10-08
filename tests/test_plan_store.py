"""계획 저장소 테스트: 다른 과제의 확정 계획을 이미 찬 시간으로 취급하는지 확인."""
from datetime import date, datetime

from conftest import GOOD_PLAN, START, TASKS_JSON, TIMETABLE

from planner_system import tools

from planner_system.plan_store import PlanStore
from planner_system.context import PlanContext


def make_ctx(path, store, tmp_path):
    return PlanContext(path, TIMETABLE, START, output_dir=tmp_path / "out", verbose=False, plan_store=store)


def approve_and_export(ctx, plan=GOOD_PLAN):
    tools.submit_tasks(ctx, TASKS_JSON)
    tools.submit_plan(ctx, plan)
    assert tools.validate_plan(ctx).startswith("검증 결과: PASS")
    return tools.export_plan(ctx)


def test_saved_plan_blocks_other_assignment(assignment_file, tmp_path):
    store_path = tmp_path / "plans.json"
    out = approve_and_export(make_ctx(assignment_file, PlanStore(store_path), tmp_path))
    assert "계획 저장소" in out and store_path.exists()

    other = tmp_path / "other_hw.txt"
    other.write_text("다른 과목 과제, 마감 2026-10-02", encoding="utf-8")
    ctx2 = make_ctx(other, PlanStore(store_path), tmp_path)
    # 첫 과제의 9/29 13:00-16:00 세션이 빈 시간에서 빠진다
    assert (13 * 60, 16 * 60) not in ctx2.schedule.free_slots(date(2026, 9, 29))
    assert all(not (s < 16 * 60 and e > 13 * 60) for s, e in ctx2.schedule.free_slots(date(2026, 9, 29)))
    assert "기존 계획 2개 세션" in (tools.submit_tasks(ctx2, TASKS_JSON) and tools.get_free_slots(ctx2))
    tools.submit_plan(ctx2, GOOD_PLAN)  # 같은 시간에 배치하면
    assert "[기존 계획] assignment" in tools.validate_plan(ctx2)  # 기존 계획과 겹친다고 반려


def test_same_assignment_is_replaced_not_blocked(assignment_file, tmp_path):
    store_path = tmp_path / "plans.json"
    approve_and_export(make_ctx(assignment_file, PlanStore(store_path), tmp_path))
    ctx = make_ctx(assignment_file, PlanStore(store_path), tmp_path)
    assert not ctx.schedule.existing  # 자기 자신의 이전 계획은 막지 않는다
    assert "기존 계획을 교체" in approve_and_export(ctx)
    assert len(PlanStore(store_path).plans) == 1


def test_no_save_and_remove(assignment_file, tmp_path):
    store_path = tmp_path / "plans.json"
    ctx = make_ctx(assignment_file, PlanStore(store_path), tmp_path)
    ctx.save_plan = False
    approve_and_export(ctx)
    assert not store_path.exists()

    approve_and_export(make_ctx(assignment_file, PlanStore(store_path), tmp_path))
    store = PlanStore(store_path)
    assert "assignment.txt" in store.summary()
    assert store.remove("assignment.txt") and not store.plans


def test_past_sessions_ignored(assignment_file, tmp_path):
    store_path = tmp_path / "plans.json"
    approve_and_export(make_ctx(assignment_file, PlanStore(store_path), tmp_path))
    other = tmp_path / "later.txt"
    other.write_text("x" * 30, encoding="utf-8")
    later = PlanContext(other, TIMETABLE, datetime(2026, 10, 5, 9, 0), verbose=False, plan_store=PlanStore(store_path))
    assert not later.schedule.existing
