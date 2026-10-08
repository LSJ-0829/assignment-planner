"""시간표 저장소 (data/timetable.json): 매주 반복되는 일정과 선호 설정.

시간표 파일이 없어도 빈 시간표로 시작할 수 있고, 반복 일정을 하나씩 추가해 만들어 간다.
반복 일정마다 시작일(from)과 반복 횟수(count)를 담을 수 있어, 16주짜리 수업처럼 끝이 있는 일정은
횟수가 끝난 뒤 자동으로 빈 시간이 된다.

반복 일정 레코드 (data["classes"]):
  {"id", "name", "short"(선택, 주간 시간표에 표시할 약어), "day"(MON..SUN), "start", "end",
   "from"(선택, 첫 주의 기준 날짜), "count"(선택, 반복 횟수. 없으면 계속 반복),
   "at_school"(선택, 기본 true. false면 등교·귀가 이동 시간을 붙이지 않음)}
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .schedule import weekly_first_date, weekly_last_date
from .utils import KOR_WEEKDAYS, WEEKDAYS, to_minutes

DEFAULT_PREFERENCES = {
    "day_start": "09:00",
    "day_end": "24:00",
    "min_session_minutes": 60,
    "commute_before_minutes": 0,
    "commute_after_minutes": 0,
}


def parse_day(text: str) -> str:
    """'화', '화요일', 'TUE', 'tue' → 'TUE'."""
    t = text.strip().upper()
    if t[:3] in WEEKDAYS:
        return t[:3]
    if t[:1] in KOR_WEEKDAYS:
        return WEEKDAYS[KOR_WEEKDAYS.index(t[:1])]
    raise ValueError(f"요일을 알 수 없습니다: '{text}' (월~일 또는 MON~SUN)")


def parse_weekly(text: str) -> dict:
    """'화 18:00-21:45 인공지능프로젝트' 형식의 입력을 (요일, 시작, 종료, 이름)으로 나눈다."""
    m = re.fullmatch(r"\s*(\S+)\s+(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})\s+(.+?)\s*", text)
    if not m:
        raise ValueError(f"반복 일정 형식을 알 수 없습니다: '{text}' (예: \"화 18:00-21:45 인공지능프로젝트\")")
    return {"day": parse_day(m.group(1)), "start": m.group(2), "end": m.group(3), "name": m.group(4)}


class TimetableStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.exists = self.path.exists()
        if self.exists:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"시간표 파일이 손상되었습니다 ({self.path}): {e}") from e
        else:
            self.data = {}  # 시간표 없이 시작: 빈 시간표 + 기본 설정
        self.data.setdefault("classes", [])
        self.data.setdefault("events", [])
        self.data.setdefault("preferences", dict(DEFAULT_PREFERENCES))
        for i, entry in enumerate(self.data["classes"], 1):  # 직접 편집한 파일의 항목에도 삭제용 ID를 붙인다
            entry.setdefault("id", f"w{i}")

    @property
    def weekly(self) -> list[dict]:
        return self.data["classes"]

    def add_weekly(self, name: str, day: str, start: str, end: str, count: int | None = None,
                   start_date: str | None = None, short: str = "", at_school: bool = True) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("일정 이름을 입력하세요.")
        s, e = to_minutes(start), to_minutes(end)
        if e <= s:
            raise ValueError("종료 시각이 시작 시각보다 늦어야 합니다.")
        if count is not None and int(count) < 1:
            raise ValueError("반복 횟수는 1 이상이어야 합니다.")
        first = date.fromisoformat(start_date) if start_date else date.today()
        entry = {
            "id": datetime.now().strftime("w%Y%m%d%H%M%S%f")[:-3],
            "name": name,
            "day": parse_day(day),
            "start": f"{s // 60:02d}:{s % 60:02d}",
            "end": f"{e // 60:02d}:{e % 60:02d}",
            "from": first.isoformat(),
        }
        if short.strip():
            entry["short"] = short.strip()
        if count is not None:
            entry["count"] = int(count)
        if not at_school:
            entry["at_school"] = False
        self.weekly.append(entry)
        return entry

    def remove_weekly(self, entry_id: str) -> dict | None:
        target = next((c for c in self.weekly if c.get("id") == entry_id), None)
        if target:
            self.weekly.remove(target)
        return target

    def describe(self, entry: dict, today: date) -> dict:
        """목록 표시용 정보: 남은 횟수와 마지막 날짜를 계산해 붙인다."""
        info = {
            "id": entry["id"], "name": entry["name"], "short": entry.get("short", ""),
            "day": entry["day"].upper(), "weekday": KOR_WEEKDAYS[WEEKDAYS.index(entry["day"].upper())],
            "start": entry["start"], "end": entry["end"], "at_school": entry.get("at_school", True),
            "from": entry.get("from"), "count": entry.get("count"), "remaining": None, "last_date": None,
        }
        last = weekly_last_date(entry)
        if last is not None:
            first = weekly_first_date(entry)
            done = 0 if today <= first else min(entry["count"], ((today - first).days + 6) // 7)
            info["remaining"] = entry["count"] - done
            info["last_date"] = last.isoformat()
        return info

    def list_weekly(self, today: date) -> list[dict]:
        order = {d: i for i, d in enumerate(WEEKDAYS)}
        return sorted((self.describe(c, today) for c in self.weekly), key=lambda x: (order[x["day"]], x["start"]))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.exists = True

    def summary(self, today: date) -> str:
        items = self.list_weekly(today)
        if not items:
            return (f"등록된 반복 일정이 없습니다. ({self.path})\n"
                    '추가 예: python main.py --add-weekly "화 18:00-21:45 인공지능프로젝트" --repeat 16')
        lines = [f"매주 반복 일정 {len(items)}개 ({self.path})"]
        for x in items:
            repeat = "계속 반복" if x["count"] is None else f"{x['count']}회 중 {x['remaining']}회 남음 (~{x['last_date']})"
            place = "" if x["at_school"] else "  [학교 밖]"
            lines.append(f"- {x['id']}  {x['weekday']} {x['start']}-{x['end']}  {x['name']}  · {repeat}{place}")
        return "\n".join(lines)

