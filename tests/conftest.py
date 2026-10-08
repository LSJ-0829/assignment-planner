import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from planner_system.context import PlanContext  # noqa: E402

# 2026-09-28(월) 18:00 시작. 월: 19:00-20:00 수업, 화: 10:00-12:00 수업
TIMETABLE = {
    "classes": [
        {"name": "월요수업", "day": "MON", "start": "19:00", "end": "20:00"},
        {"name": "화요수업", "day": "TUE", "start": "10:00", "end": "12:00"},
    ],
    "events": [{"name": "약속", "date": "2026-09-30", "start": "09:00", "end": "23:00"}],
    "preferences": {"day_start": "09:00", "day_end": "23:00", "max_daily_hours": 4, "min_session_minutes": 60,
                    "meals": []},
}
START = datetime(2026, 9, 28, 18, 0)

TASKS_JSON = json.dumps([
    {"id": "T1", "title": "코드 구현", "hours": 3, "deadline": "2026-10-02 23:59", "depends_on": []},
    {"id": "T2", "title": "리포트 작성", "hours": 2, "deadline": "2026-10-02 23:59", "depends_on": ["T1"]},
], ensure_ascii=False)

GOOD_PLAN = json.dumps([
    {"task_id": "T1", "date": "2026-09-29", "start": "13:00", "end": "16:00"},
    {"task_id": "T2", "date": "2026-10-01", "start": "10:00", "end": "12:00"},
])

# 화요수업(10:00-12:00)과 겹치고, T1 시간이 부족한 계획
BAD_PLAN = json.dumps([
    {"task_id": "T1", "date": "2026-09-29", "start": "10:00", "end": "12:00"},
    {"task_id": "T2", "date": "2026-10-01", "start": "10:00", "end": "12:00"},
])


@pytest.fixture
def assignment_file(tmp_path):
    p = tmp_path / "assignment.txt"
    p.write_text("과제: 코드 구현과 리포트 작성. 마감 2026-10-02 23:59", encoding="utf-8")
    return p


@pytest.fixture
def ctx(assignment_file, tmp_path):
    return PlanContext(assignment_file, TIMETABLE, START, output_dir=tmp_path / "out", verbose=False)
