"""특정 날짜에만 있는 일회성 약속 저장소 (data/events.json).

매주 반복되는 수업·고정 일정은 timetable.json에 두고, '이번 주 금요일 저녁 약속'처럼 한 번뿐인 일정은
여기에 추가한다. 계획을 세울 때 시간표에 합쳐져 '이미 찬 시간'으로 취급된다.

약속 레코드: {"id", "name", "date"(YYYY-MM-DD), "start"(HH:MM), "end"(HH:MM)}
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .utils import parse_user_deadline, to_minutes


def parse_event(text: str, now: datetime) -> dict:
    """'10/9 18:00-20:00 저녁 약속' 형식의 입력을 약속 레코드로 바꾼다. 날짜는 10/9, 10월 9일, 2026-10-09 모두 가능."""
    m = re.fullmatch(r"\s*(.+?)\s+(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})\s+(.+?)\s*", text)
    if not m:
        raise ValueError(f"약속 형식을 알 수 없습니다: '{text}' (예: \"10/9 18:00-20:00 저녁 약속\")")
    day = parse_user_deadline(m.group(1), datetime.combine(now.date(), datetime.min.time())).date()
    return {"name": m.group(4), "date": day.isoformat(), "start": m.group(2), "end": m.group(3)}


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.events: list[dict] = []
        if self.path.exists():
            try:
                self.events = json.loads(self.path.read_text(encoding="utf-8")).get("events", [])
            except json.JSONDecodeError as e:
                raise ValueError(f"약속 저장소 파일이 손상되었습니다 ({self.path}): {e}") from e

    def add(self, name: str, day: str, start: str, end: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("약속 이름을 입력하세요.")
        date.fromisoformat(day)  # 형식 검사
        s, e = to_minutes(start), to_minutes(end)
        if e <= s:
            raise ValueError("종료 시각이 시작 시각보다 늦어야 합니다.")
        event = {
            "id": datetime.now().strftime("e%Y%m%d%H%M%S%f")[:-3],
            "name": name, "date": day,
            "start": f"{s // 60:02d}:{s % 60:02d}", "end": f"{e // 60:02d}:{e % 60:02d}",
        }
        self.events.append(event)
        self.events.sort(key=lambda x: (x["date"], x["start"]))
        return event

    def remove(self, event_id: str) -> dict | None:
        target = next((e for e in self.events if e["id"] == event_id), None)
        if target:
            self.events.remove(target)
        return target

    def upcoming(self, since: date) -> list[dict]:
        return [e for e in self.events if e["date"] >= since.isoformat()]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"events": self.events}, ensure_ascii=False, indent=2), encoding="utf-8")

    def merged_into(self, timetable: dict) -> dict:
        """시간표 사본에 일회성 약속을 합쳐 반환한다. (원본 시간표 파일은 바꾸지 않는다)"""
        merged = dict(timetable)
        merged["events"] = list(timetable.get("events", [])) + [
            {"name": e["name"], "date": e["date"], "start": e["start"], "end": e["end"]} for e in self.events
        ]
        return merged

    def summary(self, since: date) -> str:
        items = self.upcoming(since)
        if not items:
            return f"등록된 약속이 없습니다. ({self.path})"
        lines = [f"등록된 약속 {len(items)}개 ({self.path})"]
        lines += [f"- {e['id']}  {e['date']} {e['start']}-{e['end']}  {e['name']}" for e in items]
        return "\n".join(lines)
