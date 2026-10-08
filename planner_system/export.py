"""결과 파일 생성: 캘린더(.ics), 계획표(.md), 실패 보고서(.md)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .utils import fmt_day, fmt_minutes

if TYPE_CHECKING:
    from .context import PlanContext

KST = timezone(timedelta(hours=9))


def schedule_text(ctx: PlanContext) -> str:
    lines = []
    for d in sorted({s.day for s in ctx.sessions}):
        items = ", ".join(
            f"{fmt_minutes(s.start)}-{fmt_minutes(s.end)} {ctx.tasks[s.task_id].title}"
            for s in ctx.sessions if s.day == d
        )
        lines.append(f"{fmt_day(d)}: {items}")
    return "\n".join(lines)


def plan_markdown(ctx: PlanContext) -> str:
    out = ["# 과제 수행 계획", "",
           f"- 과제 파일: {ctx.assignment_path.name}",
           f"- 우선순위: {ctx.priority}",
           f"- 생성 시각: {datetime.now():%Y-%m-%d %H:%M}",
           f"- 일정 검증: {ctx.attempts}회, 작업 단계 재분해: {ctx.reanalysis_count}회", "",
           "## 작업 목록", "",
           "| ID | 작업 | 예상 시간 | 마감 | 선행 작업 |", "|---|---|---|---|---|"]
    for t in ctx.tasks.values():
        out.append(f"| {t.id} | {t.title} | {t.hours}h | {t.deadline:%Y-%m-%d %H:%M} | {', '.join(t.depends_on) or '-'} |")
    out += ["", "## 일정", "", "| 날짜 | 시간 | 작업 |", "|---|---|---|"]
    for s in ctx.sessions:
        out.append(f"| {fmt_day(s.day)} | {fmt_minutes(s.start)}-{fmt_minutes(s.end)} | {s.task_id} {ctx.tasks[s.task_id].title} |")
    if ctx.emergency:
        e = ctx.emergency
        out += ["", "## 비상 계획 안내", "",
                f"- 마감({e['deadline']:%Y-%m-%d %H:%M})까지 시간이 절대적으로 부족해, 남은 빈 시간을 전부 이 과제에 배치했습니다.",
                f"- 필요 {e['needed']:.1f}h / 배치 {e['available']:.1f}h → **{e['shortage']:.1f}h 부족**",
                "- 세부 단계는 나누지 않았습니다. 범위를 줄이거나 마감 연장을 요청하는 것을 함께 고려하세요."]
    if ctx.meal_log:
        out += ["", "## 검증 단계에서 확정한 식사 시간", ""] + [f"- {m}" for m in ctx.meal_log]
    if ctx.postponed_log:
        out += ["", "## 이 과제 때문에 미룬 기존 일정", ""] + [f"- {m}" for m in ctx.postponed_log]
        out.append("")
        out.append("> 미룬 과제의 이전 .ics 파일은 옛 일정입니다. 최신 일정은 `python main.py --week`로 확인하세요.")
    if ctx.last_warnings:
        out += ["", "## 참고 경고", ""] + [f"- {w}" for w in ctx.last_warnings]
    return "\n".join(out) + "\n"


def failure_markdown(ctx: PlanContext) -> str:
    out = ["# 계획 생성 실패 보고서", "",
           f"- 과제 파일: {ctx.assignment_path.name}",
           f"- 실패 사유: {ctx.failure_reason}",
           f"- 일정 검증: {ctx.attempts}회, 작업 단계 재분해: {ctx.reanalysis_count}회"]
    tasks = list(ctx.tasks.values())
    if tasks:
        out += ["", "## 마감별 필요 시간 vs 가용 시간", ""]
        for dl, need, avail in ctx.schedule.feasibility(tasks):
            short = "" if need <= avail + 1e-6 else f" (부족 {need - avail:.1f}h)"
            out.append(f"- ~{dl:%Y-%m-%d %H:%M}: 필요 {need:.1f}h / 가능 {avail:.1f}h{short}")
        if not ctx.schedule.is_feasible(tasks):
            out += ["", "## 제안"]
            cap = ctx.schedule.min_feasible_cap(tasks)
            if cap is not None:
                out.append(f"- 하루 최대 공부 시간을 {ctx.schedule.max_daily_hours}h → {cap}h로 늘리면 계획이 가능합니다.")
            out.append("- 작업 범위를 줄이거나, 마감 연장을 요청하거나, 고정 일정(알바 등)을 조정하세요.")
    if ctx.last_task_issues:
        out += ["", "## 작업 단계 검증에서의 지적 사항", ""] + [f"- {e}" for e in ctx.last_task_issues]
    if ctx.last_errors:
        out += ["", "## 마지막 일정 검증에서의 위반 사항", ""] + [f"- {e}" for e in ctx.last_errors]
    return "\n".join(out) + "\n"


def calendar_ics(ctx: PlanContext) -> str:
    """구글/애플 캘린더에서 가져올 수 있는 iCalendar 파일. 시각은 KST를 UTC로 변환해 기록한다."""

    def utc(d: date, minutes: int) -> str:
        local = datetime.combine(d, datetime.min.time(), KST) + timedelta(minutes=minutes)
        return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//assignment-planner//KO", "CALSCALE:GREGORIAN"]
    for i, s in enumerate(ctx.sessions):
        t = ctx.tasks[s.task_id]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{now}-{i}@assignment-planner",
            f"DTSTAMP:{now}",
            f"DTSTART:{utc(s.day, s.start)}",
            f"DTEND:{utc(s.day, s.end)}",
            f"SUMMARY:[과제] {t.title}",
            f"DESCRIPTION:{ctx.assignment_path.name} / {t.id} / 마감 {t.deadline:%Y-%m-%d %H:%M}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
