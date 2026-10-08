"""규칙 기반 검증: 작업 단계(Analyzer의 결과)와 일정(Planner의 결과)을 각각 검사한다.

- find_dependency_cycle : 작업 등록 시 바로 거부하는 구조 오류
- review_tasks          : 검증 단계에서 보는 작업 분해 품질 → 문제가 있으면 Analyzer로 반려(재분해)
- check_plan            : 일정 규칙 위반 → 문제가 있으면 Planner로 반려(재배치)
- check_meals           : 과제를 하는 날 점심·저녁 시간이 비어 있는지 → 없으면 비울 후보 시간대를 제시
- check_continuity      : 한 작업을 불필요하게 여러 번으로 끊었는지
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .schedule import Meal, Session, Task, free_intervals, meal_candidates
from .utils import fmt_day, fmt_minutes

if TYPE_CHECKING:
    from .context import PlanContext

MAX_TASK_HOURS = 8  # 한 작업이 이보다 길면 더 작은 단계로 나눠야 한다
MIN_TASKS_FOR_LARGE_WORK = 2  # 총 작업량이 클 때 필요한 최소 단계 수
LARGE_WORK_HOURS = 3
MEAL_BREAK_MINUTES = 90  # 같은 날 이 간격 이내로 이어지는 세션은 '끊기지 않은 것'으로 본다 (식사·휴식)


@dataclass
class MealIssue:
    """식사 시간이 확보되지 않은 날. candidates: 비울 수 있는 (시작 분, 설명) 후보, 권장 순서."""
    day: date
    meal: Meal
    candidates: list[tuple[int, str]]

    def describe(self) -> str:
        m = self.meal
        options = ", ".join(f"{fmt_minutes(s)}-{fmt_minutes(s + m.minutes)}{label}" for s, label in self.candidates)
        return (f"{fmt_day(self.day)} {m.name}: {fmt_minutes(m.start)}-{fmt_minutes(m.end)} 사이에 "
                f"{m.minutes}분의 빈 시간이 없습니다. 비울 수 있는 후보: {options}")


def find_dependency_cycle(tasks: dict[str, Task]) -> list[str] | None:
    """선행 관계가 순환하면 순환 경로를 반환한다."""
    state: dict[str, int] = {}  # 1: 방문 중, 2: 완료

    def visit(tid: str, path: list[str]) -> list[str] | None:
        if state.get(tid) == 2:
            return None
        if state.get(tid) == 1:
            return path[path.index(tid):] + [tid]
        state[tid] = 1
        for dep in tasks[tid].depends_on:
            if dep in tasks:
                cycle = visit(dep, path + [tid])
                if cycle:
                    return cycle
        state[tid] = 2
        return None

    for tid in tasks:
        cycle = visit(tid, [])
        if cycle:
            return cycle
    return None


def review_tasks(tasks: dict[str, Task]) -> list[str]:
    """작업 분해 자체의 문제를 찾는다. 일정이 아니라 '단계를 어떻게 나눴는가'에 대한 검사."""
    issues = []
    total = sum(t.hours for t in tasks.values())
    for t in tasks.values():
        if t.hours > MAX_TASK_HOURS:
            issues.append(f"{t.id}({t.title}) {t.hours}h: 한 작업이 {MAX_TASK_HOURS}시간을 넘습니다. 더 작은 단계로 나누세요.")
    if len(tasks) < MIN_TASKS_FOR_LARGE_WORK and total > LARGE_WORK_HOURS:
        issues.append(f"총 {total:.1f}h 분량인데 작업이 {len(tasks)}개뿐입니다. 진행 단계별로 나누세요.")
    titles = [t.title.strip() for t in tasks.values()]
    for title in sorted({x for x in titles if titles.count(x) > 1}):
        issues.append(f"같은 이름의 작업이 여러 개입니다: '{title}'. 단계가 구분되도록 이름과 범위를 나누세요.")
    return issues


def check_plan(ctx: PlanContext) -> tuple[list[str], list[str]]:
    """일정 검증. (위반 사항, 경고)를 반환한다.

    규칙: 작업 ID, 활동 시간대, 수업·고정 일정·이동 시간·다른 과제 계획과의 충돌, 마감, 세션 간 중복,
    하루 상한 또는 권장 시간, 작업별 필요 시간, 선행 작업 순서.
    """
    sch = ctx.schedule
    errors: list[str] = []
    warnings: list[str] = []
    daily: dict[date, float] = {}
    by_task: dict[str, list[Session]] = {}

    for s in ctx.sessions:
        if s.task_id not in ctx.tasks:
            errors.append(f"[{s.label()}] 존재하지 않는 작업 ID입니다.")
            continue
        if s.end <= s.start:
            errors.append(f"[{s.label()}] 종료 시각이 시작 시각보다 빠릅니다.")
            continue
        task = ctx.tasks[s.task_id]
        by_task.setdefault(s.task_id, []).append(s)
        daily[s.day] = daily.get(s.day, 0) + s.hours
        lo, hi = sch.window(s.day)
        if s.day < ctx.start.date() or s.start < lo:
            errors.append(f"[{s.label()}] 계획 시작 시점/활동 시작 시각({fmt_minutes(lo)}) 이전입니다.")
        if s.end > hi:
            errors.append(f"[{s.label()}] 활동 종료 시각({fmt_minutes(hi)}) 이후입니다.")
        for bs, be, name in sch.busy_intervals(s.day):
            if s.start < be and bs < s.end:
                errors.append(f"[{s.label()}] '{name}'({fmt_minutes(bs)}-{fmt_minutes(be)}) 일정과 겹칩니다.")
        if s.end_datetime > task.deadline:
            errors.append(f"[{s.label()}] 마감({task.deadline:%Y-%m-%d %H:%M}) 이후에 배치되었습니다.")
        if s.end - s.start < 30:
            warnings.append(f"[{s.label()}] 30분 미만의 짧은 세션입니다.")

    valid = [s for s in ctx.sessions if s.task_id in ctx.tasks]  # 이미 (day, start) 순으로 정렬됨
    for a, b in zip(valid, valid[1:]):
        if a.day == b.day and b.start < a.end:
            errors.append(f"[{a.label()}]와 [{b.label()}] 세션이 서로 겹칩니다.")

    limit = sch.daily_limit(list(ctx.tasks.values()))
    if limit is not None:
        for d, h in sorted(daily.items()):
            if h <= limit + 1e-6:
                continue
            if sch.max_daily_hours is not None:
                errors.append(f"{fmt_day(d)} 총 {h:.1f}h로 하루 최대 {sch.max_daily_hours}h를 초과합니다.")
            else:
                errors.append(f"{fmt_day(d)} 총 {h:.1f}h 배치: 하루 권장 {limit:g}h를 넘습니다. "
                              "마감까지 여유가 있으니 넘는 만큼 다른 날로 옮기세요.")

    for t in ctx.tasks.values():
        planned = sum(s.hours for s in by_task.get(t.id, []))
        if planned + 0.01 < t.hours:
            errors.append(f"{t.id}({t.title}) 배정 {planned:.1f}h < 필요 {t.hours}h ({t.hours - planned:.1f}h 부족)")
        elif planned > t.hours + 2:
            warnings.append(f"{t.id} 배정 {planned:.1f}h가 필요 시간 {t.hours}h보다 많습니다.")
        for dep in t.depends_on:
            mine, theirs = by_task.get(t.id), by_task.get(dep)
            if not mine or not theirs:
                continue
            starts_before = (mine[0].day, mine[0].start) < (theirs[0].day, theirs[0].start)
            ends_before = (mine[-1].day, mine[-1].end) < (theirs[-1].day, theirs[-1].end)
            if starts_before or ends_before:
                errors.append(f"{t.id}는 선행 작업 {dep} 이후에 진행되어야 합니다 (순서 위반).")
    return errors, warnings


# ----- 식사 시간 ----------------------------------------------------------------
def check_meals(ctx: PlanContext) -> tuple[list[MealIssue], list[str]]:
    """새 계획이 있는 날마다 식사할 시간이 남아 있는지 본다. (식사 문제, 경고)를 반환한다.

    - 식사 시간대 안에 빈 시간이 있으면 통과.
    - 수업이 시간대를 막고 있으면, 수업 직전(이른 식사)이나 직후(늦은 식사) 중 한 곳이라도 비어 있으면 통과.
    - 계획이 그 자리를 모두 차지했으면 문제로 보고, 비울 수 있는 후보를 제시한다. (어느 쪽을 비울지는 Validator가 판단)
    """
    sch = ctx.schedule
    issues: list[MealIssue] = []
    warnings: list[str] = []
    now = ctx.start.hour * 60 + ctx.start.minute
    for d in sorted({s.day for s in ctx.sessions}):
        mine = [(s.start, s.end) for s in ctx.sessions if s.day == d and s.end > s.start]
        fixed = sch.occupied_for_meal(d)
        today = d == ctx.start.date()
        for meal in sch.meals:
            if sch.reserved_meal(d, meal):
                continue  # 이미 확정한 식사 시간과 겹치는지는 일정 충돌 검사에서 본다
            if today and meal.end <= now:
                continue  # 이미 지난 식사 시간대
            candidates = [(s, label) for s, label in meal_candidates(meal, fixed) if not (today and s < now)]
            if not candidates:
                if not today:
                    warnings.append(f"{fmt_day(d)} {meal.name}: 수업·고정 일정 때문에 식사할 시간을 찾지 못했습니다.")
                continue
            in_window = free_intervals(meal.start, meal.end, fixed + mine)
            if any(e - s >= meal.minutes for s, e in in_window):
                continue
            # 시간대 안에는 자리가 없다. 후보 자리 중 계획이 건드리지 않은 곳이 있으면 거기서 식사할 수 있다.
            if any(all(not (s < me and ms < s + meal.minutes) for ms, me in mine) for s, _ in candidates):
                continue
            issues.append(MealIssue(d, meal, candidates))
    return issues, warnings


# ----- 작업 연속성 ---------------------------------------------------------------
def check_continuity(ctx: PlanContext) -> list[str]:
    """이어서 할 수 있는데도 한 작업을 여러 번으로 끊은 경우를 찾는다.

    같은 날 식사·수업 때문에 잠깐 끊긴 것은 끊긴 것으로 보지 않는다.
    """
    sch = ctx.schedule
    notes = []
    for t in ctx.tasks.values():
        mine = [s for s in ctx.sessions if s.task_id == t.id and s.end > s.start]
        pieces = 1 if mine else 0
        for a, b in zip(mine, mine[1:]):
            if a.day != b.day:
                pieces += 1
                continue
            gap = b.start - a.end
            covered = sum(min(be, b.start) - max(bs, a.end) for bs, be, _ in sch.busy_intervals(a.day)
                          if min(be, b.start) > max(bs, a.end))
            if gap > MEAL_BREAK_MINUTES and covered < gap - MEAL_BREAK_MINUTES:
                pieces += 1
        if pieces <= 1:
            continue
        need = int(t.hours * 60)
        limit = sch.daily_limit(list(ctx.tasks.values()))
        if limit is not None and t.hours > limit:
            continue  # 하루에 배치할 수 있는 시간보다 긴 작업은 나눌 수밖에 없다
        room = None
        for d in sch.days_until(t.deadline.date()):
            run_start, run_total, prev_end = None, 0, None
            for s0, e0 in sch.free_slots(d, until=t.deadline):
                if prev_end is None or s0 - prev_end > MEAL_BREAK_MINUTES:
                    run_start, run_total = s0, 0  # 식사보다 긴 공백이면 새로 센다
                run_total += e0 - s0
                prev_end = e0
                if run_total >= need:
                    room = (d, run_start, e0)
                    break
            if room:
                break
        if room:
            notes.append(f"{t.id}({t.title})가 {pieces}번으로 떨어져 있습니다. 하루 안에 끝낼 수 있는 날이 있으니, "
                         "선행 작업 순서를 지키면서 같은 날에 이어서 배치하세요. "
                         "식사 시간이나 수업을 사이에 두고 이어 가는 것은 괜찮습니다.")
    return notes
