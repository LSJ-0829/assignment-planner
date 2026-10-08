"""승인된 계획을 누적 저장하는 계획 저장소 (data/plans.json).

과제가 나올 때마다 계획을 하나씩 추가하는 사용 흐름을 위해, 이미 확정된 다른 과제의 세션을
다음 계획에서 '이미 찬 시간'으로 취급할 수 있게 한다.

계획 레코드: {"id", "assignment", "created", "deadline", "priority", "sessions": [...]}
세션 레코드: {"task_id", "title", "date", "start", "end", "deadline"(작업 마감, 없으면 계획 마감)}
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

PRIORITIES = ("low", "normal", "high")
SessionRecord = dict  # 위 세션 레코드 형식


def priority_rank(priority: str | None) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else PRIORITIES.index("normal")


def session_hours(s: SessionRecord) -> float:
    def minutes(hhmm: str) -> int:
        return int(hhmm[:2]) * 60 + int(hhmm[3:])

    return (minutes(s["end"]) - minutes(s["start"])) / 60


class PlanStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"plans": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"계획 저장소 파일이 손상되었습니다 ({self.path}): {e}") from e
        data.setdefault("plans", [])
        return data

    @property
    def plans(self) -> list[dict]:
        return self.data["plans"]

    def find(self, assignment: str) -> dict | None:
        return next((p for p in self.plans if p["assignment"] == assignment), None)

    def others(self, assignment: str) -> list[dict]:
        """해당 과제를 제외한 다른 과제의 계획."""
        return [p for p in self.plans if p["assignment"] != assignment]

    def busy_by_date(
        self, exclude_assignment: str, since: date, overrides: dict[str, list[SessionRecord]] | None = None
    ) -> dict[date, list[tuple[str, str, str]]]:
        """다른 과제의 세션을 날짜별 (start, end, 이름)으로 반환한다. 지난 날짜는 제외.

        overrides: {계획 ID: 세션 목록} — 아직 저장하지 않은 '미룬 일정'을 저장된 세션 대신 사용한다.
        같은 과제를 다시 계획하는 경우 이전 계획은 비워 둔다(승인되면 교체).
        """
        out: dict[date, list[tuple[str, str, str]]] = {}
        for plan in self.others(exclude_assignment):
            label = Path(plan["assignment"]).stem
            for s in (overrides or {}).get(plan["id"], plan["sessions"]):
                d = date.fromisoformat(s["date"])
                if d >= since:
                    title = s.get("title", s["task_id"])
                    out.setdefault(d, []).append((s["start"], s["end"], f"[기존 계획] {label} {title[:20]}"))
        return out

    def upsert(self, assignment: str, sessions: list[SessionRecord], deadline: datetime, priority: str = "normal") -> bool:
        """계획을 저장한다. 같은 과제의 계획이 이미 있으면 교체하고 True를 반환한다."""
        old = self.find(assignment)
        record = {
            "id": datetime.now().strftime("p%Y%m%d%H%M%S"),
            "assignment": assignment,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "deadline": deadline.strftime("%Y-%m-%d %H:%M"),
            "priority": priority,
            "sessions": sessions,
        }
        if old:
            self.plans[self.plans.index(old)] = record
        else:
            self.plans.append(record)
        return old is not None

    def set_sessions(self, plan_id: str, sessions: list[SessionRecord]) -> None:
        """기존 계획의 세션을 바꾼다 (다른 과제 때문에 미룬 일정 반영)."""
        for plan in self.plans:
            if plan["id"] == plan_id:
                plan["sessions"] = sorted(sessions, key=lambda s: (s["date"], s["start"]))
                plan["rescheduled"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    def remove(self, key: str) -> dict | None:
        """계획 ID 또는 과제 파일 이름으로 계획을 삭제한다."""
        target = next((p for p in self.plans if key in (p["id"], p["assignment"])), None)
        if target:
            self.plans.remove(target)
        return target

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def summary(self) -> str:
        if not self.plans:
            return f"저장된 계획이 없습니다. ({self.path})"
        lines = [f"저장된 계획 {len(self.plans)}개 ({self.path})"]
        for p in self.plans:
            total = sum(session_hours(s) for s in p["sessions"])
            days = sorted({s["date"] for s in p["sessions"]})
            span = f"{days[0]} ~ {days[-1]}" if days else "-"
            moved = "  (미룬 적 있음)" if p.get("rescheduled") else ""
            lines.append(f"- {p['id']}  {p['assignment']}  마감 {p['deadline']}  우선순위 {p.get('priority', 'normal')}  "
                         f"세션 {len(p['sessions'])}개/{total:.1f}h  ({span}){moved}")
        return "\n".join(lines)
