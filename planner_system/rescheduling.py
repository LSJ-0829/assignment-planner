"""긴박한 과제를 위해 다른 과제의 기존 계획을 뒤로 미루는 로직.

새 과제의 마감 전까지 시간이 부족할 때만 동작한다. 미룰 수 있는 것은
  - 우선순위가 새 과제보다 낮거나 같은 과제의 계획 중 (낮은 우선순위 → 마감이 먼 순서로 선택)
  - 아직 시작하지 않은 세션이며
  - 옮긴 뒤에도 그 작업의 마감 안에 들어가는 경우뿐이다.

한 과제 안의 작업 순서를 지키기 위해, 옮기는 세션과 그 뒤의 세션을 순서대로 다시 배치한다.
결과는 ctx.postponed에만 기록하고, 새 계획이 승인될 때 저장소에 반영한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .plan_store import SessionRecord, priority_rank, session_hours
from .utils import fmt_minutes, parse_deadline, to_minutes

if TYPE_CHECKING:
    from .context import PlanContext


def current_shortage(ctx: PlanContext) -> tuple[datetime, float] | None:
    """부족한 마감이 있으면 (부족이 발생한 가장 늦은 마감, 가장 큰 부족 시간)을 반환한다."""
    rows = [
        (dl, need - avail)
        for dl, need, avail in ctx.schedule.feasibility(list(ctx.tasks.values()))
        if need > avail + 1e-6
    ]
    if not rows:
        return None
    return max(dl for dl, _ in rows), max(short for _, short in rows)


def _start_dt(s: SessionRecord) -> datetime:
    return datetime.fromisoformat(s["date"]) + timedelta(minutes=to_minutes(s["start"]))


def _span(s: SessionRecord) -> str:
    return f"{s['date'][5:]} {s['start']}-{s['end']}"


def _candidates(ctx: PlanContext) -> list[dict]:
    """미룰 수 있는 다른 과제의 계획. 덜 중요한 것(낮은 우선순위, 먼 마감)부터."""
    if ctx.plan_store is None:
        return []
    rank = priority_rank(ctx.priority)
    plans = [p for p in ctx.plan_store.others(ctx.assignment_path.name) if priority_rank(p.get("priority")) <= rank]
    return sorted(plans, key=lambda p: (priority_rank(p.get("priority")), -parse_deadline(p["deadline"]).timestamp()))


def _find_slot(ctx: PlanContext, earliest: datetime, minutes: int, deadline: datetime) -> tuple[datetime, int] | None:
    """earliest 이후, deadline 이전에 minutes 길이가 들어가는 가장 빠른 빈 시간 (날짜, 시작 분)."""
    day = earliest.date()
    while day <= deadline.date():
        for start, end in ctx.schedule.free_slots(day, until=deadline):
            if day == earliest.date():
                start = max(start, earliest.hour * 60 + earliest.minute)
            if end - start >= minutes:
                return datetime.combine(day, datetime.min.time()), start
        day += timedelta(days=1)
    return None


def _repack(ctx: PlanContext, plan: dict, kept: list[SessionRecord], moving: list[SessionRecord],
            after: datetime) -> list[SessionRecord] | None:
    """moving 세션들을 after 이후의 빈 시간에 순서대로 다시 배치한다. 하나라도 마감을 넘기면 None(원상 복구)."""
    backup = ctx.postponed.get(plan["id"])
    placed: list[SessionRecord] = []
    cursor = after
    for s in moving:
        ctx.postponed[plan["id"]] = kept + placed  # 옮기는 세션의 원래 자리는 비운 상태로 탐색
        ctx.refresh_existing()
        minutes = to_minutes(s["end"]) - to_minutes(s["start"])
        deadline = parse_deadline(s.get("deadline") or plan["deadline"])
        slot = _find_slot(ctx, max(cursor, _start_dt(s)), minutes, deadline)  # 원래보다 앞당기지는 않는다
        if slot is None:
            if backup is None:
                ctx.postponed.pop(plan["id"], None)
            else:
                ctx.postponed[plan["id"]] = backup
            ctx.refresh_existing()
            return None
        day, start = slot
        placed.append({**s, "date": day.date().isoformat(), "start": fmt_minutes(start), "end": fmt_minutes(start + minutes)})
        cursor = day + timedelta(minutes=start + minutes)
    ctx.postponed[plan["id"]] = kept + placed
    ctx.refresh_existing()
    return placed


def _postpone_once(ctx: PlanContext, plan: dict, window_end: datetime, need: float) -> bool:
    """한 과제의 계획에서 need 시간만큼(가능한 만큼) window_end 뒤로 옮긴다. 옮겼으면 True."""
    sessions = sorted(ctx.postponed.get(plan["id"], plan["sessions"]), key=lambda s: (s["date"], s["start"]))
    done = [s for s in sessions if _start_dt(s) < ctx.start]  # 이미 지난 세션은 건드리지 않는다
    future = sessions[len(done):]
    in_window = [i for i, s in enumerate(future) if _start_dt(s) < window_end]
    if not in_window:
        return False
    # 부족분을 채울 만큼만, 창 안의 뒤쪽 세션부터 옮긴다. (그 뒤의 세션은 순서 유지를 위해 함께 재배치)
    freed, cut = 0.0, in_window[-1]
    for i in reversed(in_window):
        freed += session_hours(future[i])
        cut = i
        if freed >= need - 1e-6:
            break
    return _repack(ctx, plan, done + future[:cut], future[cut:], window_end) is not None


def describe_changes(ctx: PlanContext) -> list[str]:
    """저장된 원래 일정과 미룬 일정을 비교해 '무엇이 언제로 옮겨졌는지'를 문장으로 만든다."""
    if ctx.plan_store is None:
        return []
    lines = []
    for plan in ctx.plan_store.plans:
        if plan["id"] not in ctx.postponed:
            continue
        label = Path(plan["assignment"]).stem
        before = sorted(plan["sessions"], key=lambda s: (s["date"], s["start"]))
        after = sorted(ctx.postponed[plan["id"]], key=lambda s: (s["date"], s["start"]))
        for old, new in zip(before, after):  # 순서를 유지한 채 옮기므로 같은 위치끼리 대응한다
            if (old["date"], old["start"]) != (new["date"], new["start"]):
                lines.append(f"{label} · {old.get('title', old['task_id'])}: {_span(old)} → {_span(new)}")
    return lines


def postpone_existing_plans(ctx: PlanContext) -> list[str]:
    """시간 부족이 해소되거나 더 옮길 것이 없을 때까지 기존 계획을 미룬다. 옮긴 전체 내역을 반환한다."""
    progress = True
    while progress:
        progress = False
        for plan in _candidates(ctx):
            shortage = current_shortage(ctx)
            if shortage is None:
                break
            if _postpone_once(ctx, plan, *shortage):
                progress = True
        if current_shortage(ctx) is None:
            break
    ctx.postponed_log = describe_changes(ctx)
    return ctx.postponed_log


def can_resolve_by_postponing(ctx: PlanContext) -> bool:
    """기존 계획을 미루면 시간 부족이 해소되는지 미리 계산해 본다. (상태는 바꾸지 않음)"""
    if current_shortage(ctx) is None or not _candidates(ctx):
        return False
    snapshot = {k: list(v) for k, v in ctx.postponed.items()}
    log = list(ctx.postponed_log)
    postpone_existing_plans(ctx)
    resolved = current_shortage(ctx) is None
    ctx.postponed, ctx.postponed_log = snapshot, log
    ctx.refresh_existing()
    return resolved


def repair_conflicts(plan_store, timetable: dict, now: datetime) -> tuple[list[str], list[str]]:
    """약속을 새로 추가한 뒤, 그 약속과 겹치게 된 기존 과제 계획을 다음 빈 시간으로 옮긴다. (API 불필요)

    겹치는 세션과 그 뒤의 세션을 순서대로, 각 작업의 마감 안에서 다시 배치하고 저장한다.
    반환: (옮긴 내역, 마감 때문에 옮기지 못한 충돌)
    """
    from .context import PlanContext  # 순환 import 방지

    ctx = PlanContext("(일정 조정)", timetable, now, verbose=False, plan_store=plan_store)
    unresolved: list[str] = []
    for plan in plan_store.plans:
        sessions = sorted(plan["sessions"], key=lambda s: (s["date"], s["start"]))
        done = [s for s in sessions if _start_dt(s) < now]
        future = sessions[len(done):]
        first_conflict = None
        for i, s in enumerate(future):
            day = datetime.fromisoformat(s["date"]).date()
            start, end = to_minutes(s["start"]), to_minutes(s["end"])
            # 시간표(수업·약속·이동)와의 충돌만 본다. 다른 과제 계획과의 관계는 그대로 둔다.
            clash = next((name for bs, be, name in ctx.schedule.busy_intervals(day)
                          if not name.startswith("[기존 계획]") and start < be and bs < end), None)
            if clash:
                first_conflict = (i, clash)
                break
        if first_conflict is None:
            continue
        cut, clash = first_conflict
        if _repack(ctx, plan, done + future[:cut], future[cut:], now) is None:
            s = future[cut]
            label = Path(plan["assignment"]).stem
            unresolved.append(f"{label} · {s.get('title', s['task_id'])} ({_span(s)})이 '{clash}'와 겹치지만, "
                              "마감 안에 옮길 빈 시간이 없습니다.")
    moved = describe_changes(ctx)
    for plan_id, sessions in ctx.postponed.items():
        plan_store.set_sessions(plan_id, sessions)
    if ctx.postponed:
        plan_store.save()
    return moved, unresolved
