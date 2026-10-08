"""주간 시간표 시각화 테스트 (API 불필요)."""
from datetime import date, datetime

from conftest import TIMETABLE

from planner_system.plan_store import PlanStore
from planner_system.week_view import collect_blocks, render_html, render_terminal, week_of


def test_week_view_includes_classes_events_and_saved_plans(tmp_path):
    store = PlanStore(tmp_path / "plans.json")
    store.upsert("hw.pdf", [{"task_id": "T1", "title": "문제 풀이", "date": "2026-10-01",
                             "start": "13:00", "end": "15:00"}], deadline=datetime(2026, 10, 2))
    days = week_of(date(2026, 9, 30))
    assert days[0] == date(2026, 9, 28) and days[-1] == date(2026, 10, 4)

    blocks = collect_blocks(TIMETABLE, store, days)
    kinds = {(b.name, b.kind) for b in blocks}
    assert ("월요수업", "class") in kinds and ("약속", "event") in kinds and ("문제 풀이", "plan") in kinds

    text = render_terminal(blocks, days, 9 * 60, 24 * 60, date(2026, 9, 30))
    assert "10/1(목)" in text and "범례" in text
    page = render_html(blocks, days, 9 * 60, 24 * 60, date(2026, 9, 30))
    assert "문제 풀이" in page and "13:00-15:00" in page
