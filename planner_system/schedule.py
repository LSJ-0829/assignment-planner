"""계획 데이터 구조(Task, Session)와 시간표 기반 가용 시간 계산(Schedule)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .utils import WEEKDAYS, fmt_day, fmt_minutes, to_minutes

Interval = tuple[int, int, str]  # (시작 분, 종료 분, 일정 이름)

COMMUTE_NAMES = ("등교 준비·이동", "귀가 이동")
MEAL_SUFFIX = " 식사"  # 예약된 식사 시간의 이름: "점심 식사", "저녁 식사"
# 기본 방침은 '가능한 한 빨리 끝내기'라 하루 권장 시간은 쓰지 않는다. (급한 과제가 생기면 기존 계획을 미뤄 재조정)
# 시간표의 recommended_daily_hours로 켜면, 마감에 여유가 있을 때 하루에 몰아서 배치하지 않게 한다.
DEFAULT_RECOMMENDED_DAILY_HOURS = None
MAX_RELAXED_DAILY_HOURS = 16
DEFAULT_MEALS = [
    {"name": "점심", "from": "11:30", "to": "14:00", "minutes": 60, "prefer": "12:00"},
    {"name": "저녁", "from": "17:30", "to": "20:30", "minutes": 60, "prefer": "18:00"},
]


@dataclass
class Meal:
    """과제를 하는 날 비워 둬야 하는 식사 시간. [start, end] 사이에 minutes만큼의 빈 시간이 있어야 한다."""
    name: str
    start: int
    end: int
    minutes: int
    prefer: int


@dataclass
class Task:
    id: str
    title: str
    hours: float
    deadline: datetime
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Session:
    task_id: str
    day: date
    start: int  # 자정 기준 분
    end: int

    @property
    def hours(self) -> float:
        return (self.end - self.start) / 60

    @property
    def end_datetime(self) -> datetime:
        return datetime.combine(self.day, datetime.min.time()) + timedelta(minutes=self.end)

    def label(self) -> str:
        return f"{self.task_id} {fmt_day(self.day)} {fmt_minutes(self.start)}-{fmt_minutes(self.end)}"


def weekly_first_date(entry: dict) -> date | None:
    """반복 일정의 첫 번째 날짜. 시작일(from)이 없으면 None(처음부터 계속 반복)."""
    if not entry.get("from"):
        return None
    start = date.fromisoformat(entry["from"])
    return start + timedelta(days=(WEEKDAYS.index(entry["day"].upper()) - start.weekday()) % 7)


def weekly_last_date(entry: dict) -> date | None:
    """반복 횟수(count)가 있는 일정의 마지막 날짜. 횟수가 없으면 None(끝없이 반복)."""
    first = weekly_first_date(entry)
    if first is None or not entry.get("count"):
        return None
    return first + timedelta(weeks=int(entry["count"]) - 1)


def weekly_active(entry: dict, d: date) -> bool:
    """요일이 맞고, 시작일 이후이며, 반복 횟수 안에 드는 날인가."""
    if entry.get("day", "").upper() != WEEKDAYS[d.weekday()]:
        return False
    first, last = weekly_first_date(entry), weekly_last_date(entry)
    return (first is None or d >= first) and (last is None or d <= last)


MEAL_SEARCH_START, MEAL_SEARCH_END = 7 * 60, 24 * 60  # 식사 시간대 밖에서 대안을 찾는 범위
MEAL_SHIFT_LIMIT = 60  # 식사를 시간대에서 이만큼(분)까지만 앞당기거나 늦춘다


def free_intervals(lo: int, hi: int, occupied: list[tuple[int, int]]) -> list[tuple[int, int]]:
    free, cur = [], lo
    for s, e in sorted(occupied):
        if s > cur:
            free.append((cur, min(s, hi)))
        cur = max(cur, e)
        if cur >= hi:
            break
    if cur < hi:
        free.append((cur, hi))
    return [(s, e) for s, e in free if e > s]


def meal_candidates(meal: Meal, fixed: list[tuple[int, int]]) -> list[tuple[int, str]]:
    """수업·고정 일정(fixed)을 피해 식사할 수 있는 시작 시각 후보.

    식사 시간대 안에 자리가 있으면 선호 시각에 가까운 순으로, 없으면 시간대 직전(이른 식사)과 직후(늦은 식사)를 제시한다.
    """
    inside = [(a, b) for a, b in free_intervals(meal.start, meal.end, fixed) if b - a >= meal.minutes]
    if inside:
        starts = {min(max(meal.prefer, a), b - meal.minutes) for a, b in inside}
        starts |= {a for a, b in inside} | {b - meal.minutes for a, b in inside}
        return [(s, "") for s in sorted(starts, key=lambda x: (abs(x - meal.prefer), x))[:3]]
    # 시간대 안에 자리가 없으면, 시간대에 가장 가까운 이른 자리와 늦은 자리를 찾는다. (2시간 이내)
    out = []
    around = free_intervals(MEAL_SEARCH_START, MEAL_SEARCH_END, fixed)
    early = [min(b, meal.end) - meal.minutes for a, b in around
             if a < meal.start and min(b, meal.end) - meal.minutes >= max(a, meal.start - MEAL_SHIFT_LIMIT)]
    late = [max(a, meal.start) for a, b in around
            if b > meal.end and max(a, meal.start) + meal.minutes <= min(b, meal.end + MEAL_SHIFT_LIMIT + meal.minutes)]
    if early:
        out.append((max(early), " (이른 식사)"))
    if late:
        out.append((min(late), " (늦은 식사)"))
    return out


class Schedule:
    """시간표(수업·고정 일정)와 선호 설정으로 날짜별 바쁜 시간과 빈 시간을 계산한다.

    preferences
      day_start / day_end        과제를 배치할 수 있는 시간대 (기본 09:00-24:00)
      min_session_minutes        이보다 짧은 빈 시간에는 배치하지 않음 (기본 60)
      commute_before/after_minutes  학교 가는 날 첫 일정 전 / 마지막 일정 후 이동 시간
      max_daily_hours (선택)     하루 과제 시간 상한. 없으면 제한 없음
      recommended_daily_hours (선택) 하루 권장 과제 시간. 기본은 사용 안 함. 넣으면 여유가 있을 때 여러 날로 나누고,
                                 마감이 촉박해 불가능하면 자동으로 늘어난다
      meals (선택)               식사 시간대 목록. 생략하면 점심 11:30-14:00, 저녁 17:30-20:30 사이 60분. []이면 사용 안 함
    """

    def __init__(self, timetable: dict, start: datetime, existing: dict[date, list[Interval]] | None = None):
        prefs = timetable.get("preferences", {})
        self.classes = timetable.get("classes", [])
        self.events = timetable.get("events", [])
        self.day_start = to_minutes(prefs.get("day_start", "09:00"))
        self.day_end = to_minutes(prefs.get("day_end", "24:00"))
        self.min_session = int(prefs.get("min_session_minutes", 60))
        self.commute_before = int(prefs.get("commute_before_minutes", 0))
        self.commute_after = int(prefs.get("commute_after_minutes", 0))
        cap = prefs.get("max_daily_hours")
        self.max_daily_hours: float | None = float(cap) if cap is not None else None
        rec = prefs.get("recommended_daily_hours", DEFAULT_RECOMMENDED_DAILY_HOURS)
        self.recommended_daily_hours: float | None = float(rec) if rec else None
        self.meals = [
            Meal(m["name"], to_minutes(m["from"]), to_minutes(m["to"]), int(m.get("minutes", 60)),
                 to_minutes(m.get("prefer", m["from"])))
            for m in prefs.get("meals", DEFAULT_MEALS)
        ]
        self.start = start
        self.existing = existing or {}  # 다른 과제의 확정된 세션 (계획 저장소)
        self.reserved: dict[date, list[Interval]] = {}  # 검증 단계에서 비우기로 확정한 식사 시간

    # ----- 하루 단위 계산 -----------------------------------------------------
    def busy_intervals(self, d: date) -> list[Interval]:
        busy: list[Interval] = []
        at_school: list[Interval] = []
        for c in self.classes:  # 매주 반복 일정 (시작일·반복 횟수가 있으면 그 기간에만)
            if weekly_active(c, d):
                busy.append((to_minutes(c["start"]), to_minutes(c["end"]), c["name"]))
                if c.get("at_school", True):
                    at_school.append(busy[-1])
        for e in self.events:  # 특정 날짜의 일정, 또는 day로 지정한 반복 일정
            if e.get("date") == d.isoformat() or ("day" in e and weekly_active(e, d)):
                busy.append((to_minutes(e["start"]), to_minutes(e["end"]), e["name"]))
                if e.get("at_school"):
                    at_school.append(busy[-1])
        if at_school:  # 학교에 가는 날: 첫 일정 전 등교, 마지막 일정 후 귀가 시간을 비운다
            first = min(s for s, _, _ in at_school)
            last = max(e for _, e, _ in at_school)
            if self.commute_before:
                busy.append((max(first - self.commute_before, 0), first, "등교 준비·이동"))
            if self.commute_after:
                busy.append((last, min(last + self.commute_after, 24 * 60), "귀가 이동"))
        busy.extend(self.existing.get(d, []))
        busy.extend(self.reserved.get(d, []))
        return sorted(busy)

    def occupied_for_meal(self, d: date) -> list[tuple[int, int]]:
        """식사를 할 수 없는 시간: 수업, 고정 일정, 다른 과제의 계획. (이동 시간에는 식사할 수 있다고 본다)"""
        return [(s, e) for s, e, name in self.busy_intervals(d)
                if name not in COMMUTE_NAMES and not name.endswith(MEAL_SUFFIX)]

    def reserve_default_meals(self, last_day: date) -> None:
        """계획 기간의 날마다 기본 식사 시간을 미리 비워 둔다. (Planner가 식사 시간을 따로 계산하지 않아도 되게)

        식사 시간대 안에 자리가 있으면 선호 시각에 가장 가까운 곳을 예약한다. 수업이 시간대를 막아 이른/늦은 식사만
        가능한 날은 예약하지 않고 검증 단계에서 Validator가 어느 쪽을 비울지 판단하게 둔다.
        """
        now = self.start.hour * 60 + self.start.minute
        for d in self.days_until(last_day):
            today = d == self.start.date()
            for meal in self.meals:
                if self.reserved_meal(d, meal) or (today and meal.end <= now):
                    continue
                inside = [s for s, label in meal_candidates(meal, self.occupied_for_meal(d))
                          if not label and not (today and s < now)]
                if inside:
                    self.reserved.setdefault(d, []).append((inside[0], inside[0] + meal.minutes, meal.name + MEAL_SUFFIX))

    def reserved_meal(self, d: date, meal: Meal) -> Interval | None:
        return next((r for r in self.reserved.get(d, []) if r[2] == meal.name + MEAL_SUFFIX), None)

    def window(self, d: date) -> tuple[int, int]:
        """그날 배치 가능한 시간대. 계획 시작일은 시작 시각(30분 단위 올림) 이후만."""
        lo = self.day_start
        if d == self.start.date():
            now = self.start.hour * 60 + self.start.minute
            lo = max(lo, -(-now // 30) * 30)
        return lo, self.day_end

    def free_slots(self, d: date, until: datetime | None = None) -> list[tuple[int, int]]:
        lo, hi = self.window(d)
        if until is not None and d == until.date():
            hi = min(hi, until.hour * 60 + until.minute)
        slots, cur = [], lo
        for s, e, _ in self.busy_intervals(d):
            if s > cur:
                slots.append((cur, min(s, hi)))
            cur = max(cur, e)
        slots.append((cur, hi))
        return [(s, e) for s, e in slots if e - s >= 30]

    # ----- 기간 단위 계산 -----------------------------------------------------
    def days_until(self, last: date) -> list[date]:
        n = (last - self.start.date()).days + 1
        return [self.start.date() + timedelta(days=i) for i in range(max(n, 0))]

    def available_hours(self, until: datetime, daily_cap: float | None = None) -> float:
        cap = daily_cap if daily_cap is not None else self.max_daily_hours
        cap = 24.0 if cap is None else cap
        return sum(
            min(sum(e - s for s, e in self.free_slots(d, until)) / 60, cap) for d in self.days_until(until.date())
        )

    def feasibility(self, tasks: list[Task], daily_cap: float | None = None) -> list[tuple[datetime, float, float]]:
        """마감별 (마감, 그 마감까지 필요한 누적 시간, 가용 시간)."""
        rows = []
        for dl in sorted({t.deadline for t in tasks}):
            need = sum(t.hours for t in tasks if t.deadline <= dl)
            rows.append((dl, need, self.available_hours(dl, daily_cap)))
        return rows

    def is_feasible(self, tasks: list[Task], daily_cap: float | None = None) -> bool:
        return all(need <= avail + 1e-6 for _, need, avail in self.feasibility(tasks, daily_cap))

    def min_feasible_cap(self, tasks: list[Task]) -> float | None:
        """하루 상한을 몇 시간으로 늘리면 가능한지. 상한이 없는 설정이거나 14시간으로도 불가능하면 None."""
        if self.max_daily_hours is None:
            return None
        cap = self.max_daily_hours
        while cap <= 14:
            if self.is_feasible(tasks, cap):
                return cap
            cap += 0.5
        return None

    def daily_limit(self, tasks: list[Task]) -> float | None:
        """이 작업들을 계획할 때 하루에 배치할 수 있는 시간.

        절대 상한(max_daily_hours)이 있으면 그 값. 없으면 권장 시간에서 시작해, 그 시간으로 마감을 맞출 수 없을 때만
        맞출 수 있는 가장 작은 값까지 1시간씩 늘린다. 권장 시간을 쓰지 않거나 어떻게 해도 불가능하면 None(제한 없음).
        """
        if self.max_daily_hours is not None:
            return self.max_daily_hours
        if self.recommended_daily_hours is None or not tasks:
            return None
        cap = self.recommended_daily_hours
        while cap <= MAX_RELAXED_DAILY_HOURS:
            if self.is_feasible(tasks, cap):
                return cap
            cap += 1
        return None

    def rules_text(self) -> str:
        rules = [f"활동 가능 {fmt_minutes(self.day_start)}-{fmt_minutes(self.day_end)}", f"최소 세션 {self.min_session}분"]
        if self.max_daily_hours is not None:
            rules.append(f"하루 최대 공부 {self.max_daily_hours}h")
        if self.commute_before or self.commute_after:
            rules.append(f"수업 있는 날은 첫 수업 전 {self.commute_before}분·마지막 수업 후 {self.commute_after}분 이동 시간 제외")
        if self.meals:
            rules.append(f"{'·'.join(m.name for m in self.meals)} 식사 시간은 빈 시간에서 이미 제외됨")
        n_existing = sum(len(v) for v in self.existing.values())
        if n_existing:
            rules.append(f"다른 과제의 기존 계획 {n_existing}개 세션 시간 제외")
        return ", ".join(rules)
