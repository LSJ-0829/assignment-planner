"""Agent가 호출하는 Tool 모음.

LLM이 틀리기 쉬운 작업(파일 읽기, 빈 시간 계산, 일정 이동, 충돌·마감·합계 검사, 파일 생성)은
모두 결정적인 Python 코드가 담당한다. 각 함수는 공유 상태(PlanContext)를 받아 결과를 문자열로 반환하며,
예외를 던지지 않고 '오류: ...' 문자열을 돌려줘 LLM이 스스로 고칠 수 있게 한다.

| Tool                    | 호출 Agent | 호출 시점                                             |
|-------------------------|-----------|------------------------------------------------------|
| read_assignment         | Analyzer  | 분석을 시작할 때 과제 파일(PDF/텍스트) 읽기              |
| submit_tasks            | Analyzer  | 세부 작업을 나눈 뒤 등록 (재분해 요청을 받으면 다시 호출)  |
| get_free_slots          | Planner   | 배치 전에 날짜별 빈 시간과 마감별 실현 가능성 확인        |
| postpone_existing_plans | Planner   | 시간이 부족할 때 덜 중요한 다른 과제의 계획을 뒤로 미룸    |
| submit_plan             | Planner   | 세션 배치를 마친 뒤 제출 (반려되면 고쳐서 다시 호출)       |
| validate_plan           | Validator | 계획이 제출될 때마다 작업 단계와 일정을 규칙으로 검증      |
| request_reanalysis      | Validator | 과제 원문과 비교해 작업 단계 자체가 잘못됐다고 판단할 때   |
| reserve_meal            | Validator | 식사 시간이 없는 날, 비울 시간대를 판단해 확정할 때        |
| export_plan             | Exporter  | 마지막에 .ics·계획표 저장 또는 실패 보고서 작성           |
"""
from __future__ import annotations

import json
from datetime import date, datetime
from datetime import date as _date  # reserve_meal의 인자 이름(date)과 구분

from autogen_core.tools import FunctionTool

from . import export, rescheduling, validation
from .context import PlanContext
from .schedule import COMMUTE_NAMES, MEAL_SUFFIX, Session, Task
from .utils import extract_json, fmt_day, fmt_minutes, parse_deadline, to_minutes

MAX_ASSIGNMENT_CHARS = 8000  # Analyzer에게 전달하는 과제 원문 최대 길이
EXCERPT_CHARS = 2500  # Validator가 작업 단계를 대조할 때 보는 과제 원문 발췌 길이
MAX_REPORTED_ERRORS = 15

TASKS_FORMAT = '[{"id":"T1","title":"...","hours":2,"deadline":"YYYY-MM-DD HH:MM","depends_on":[]}]'
PLAN_FORMAT = '[{"task_id":"T1","date":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM"}]'


def _task_line(t: Task) -> str:
    dep = f" (선행: {', '.join(t.depends_on)})" if t.depends_on else ""
    return f"- {t.id} {t.title}: {t.hours}h, 마감 {t.deadline:%Y-%m-%d %H:%M}{dep}"


# ----- Tool 1: read_assignment (Analyzer) -------------------------------------
def read_assignment(ctx: PlanContext) -> str:
    try:
        text = ctx.extract_text()
    except (OSError, ValueError) as e:
        ctx.failure_reason = str(e)
        return f"오류: {e}. 작업을 분석할 수 없습니다."
    if ctx.text_is_sparse() and not ctx.user_note:
        ctx.failure_reason = "과제 파일에서 텍스트를 추출하지 못함 (이미지로 된 PDF일 수 있음)"
        return "오류: 과제 파일에서 텍스트를 거의 추출하지 못했습니다. 사용자 설명(--note)이 필요합니다."

    header = [f"[계획 시작 시점] {fmt_day(ctx.start.date())} {ctx.start:%H:%M}"]
    if ctx.user_deadline:
        header.append(f"[사용자 입력 마감] {ctx.user_deadline:%Y-%m-%d %H:%M} (문서의 날짜보다 우선하는 최종 마감)")
    if ctx.user_note:
        header.append(f"[사용자 설명] {ctx.user_note}")
    if ctx.text_is_sparse():
        header.append("[주의] 문서 내용이 이미지로 되어 있어 텍스트가 거의 추출되지 않았습니다. 사용자 설명을 기준으로 분석하세요.")
    header.append(f"[과제 파일] {ctx.assignment_path.name}")
    body = text[:MAX_ASSIGNMENT_CHARS] + ("\n...(이하 생략)" if len(text) > MAX_ASSIGNMENT_CHARS else "")
    return "\n".join(header) + f"\n{'-' * 40}\n{body}"


# ----- Tool 2: submit_tasks (Analyzer) ----------------------------------------
def submit_tasks(ctx: PlanContext, tasks_json: str) -> str:
    try:
        tasks = _parse_tasks(ctx, tasks_json)
    except (KeyError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return f"오류: 작업 목록 형식이 잘못되었습니다 ({e}). {TASKS_FORMAT} 형식으로 다시 제출하세요."
    resubmitted = bool(ctx.tasks)
    ctx.tasks = tasks
    ctx.reset_plan()  # 작업이 바뀌면 이전 계획과 미룬 일정은 무효
    ctx.schedule.reserve_default_meals(max(t.deadline for t in tasks.values()).date())
    ctx.reanalysis_pending = False
    ctx.status = "planning"
    head = f"작업 {len(tasks)}개 {'재등록' if resubmitted else '등록'} 완료:"
    return "\n".join([head] + [_task_line(t) for t in tasks.values()])


def _parse_tasks(ctx: PlanContext, tasks_json: str) -> dict[str, Task]:
    data = extract_json(tasks_json)
    if isinstance(data, dict):
        data = data.get("tasks", [])
    if not isinstance(data, list) or not data:
        raise ValueError("작업 목록이 비어 있습니다.")
    tasks: dict[str, Task] = {}
    for i, raw in enumerate(data, 1):
        tid = str(raw.get("id") or f"T{i}").strip()
        hours = float(raw["hours"])
        if not 0 < hours <= 40:
            raise ValueError(f"{tid}: 예상 시간 {hours}h가 범위(0~40h)를 벗어났습니다.")
        deadline = parse_deadline(str(raw["deadline"]))
        if deadline <= ctx.start:
            raise ValueError(f"{tid}: 마감 {deadline:%Y-%m-%d %H:%M}이 계획 시작 시점보다 이전입니다.")
        if ctx.user_deadline and deadline > ctx.user_deadline:
            raise ValueError(
                f"{tid}: 마감 {deadline:%Y-%m-%d %H:%M}이 사용자 입력 마감 {ctx.user_deadline:%Y-%m-%d %H:%M}보다 늦습니다."
            )
        deps = raw.get("depends_on") or []
        deps = [deps] if isinstance(deps, str) else deps
        tasks[tid] = Task(tid, str(raw.get("title", tid)), hours, deadline, [str(x) for x in deps])
    for t in tasks.values():
        for dep in t.depends_on:
            if dep not in tasks:
                raise ValueError(f"{t.id}: 존재하지 않는 선행 작업 '{dep}'")
            if tasks[dep].deadline > t.deadline:
                raise ValueError(f"{t.id}: 선행 작업 {dep}의 마감이 더 늦습니다.")
    cycle = validation.find_dependency_cycle(tasks)
    if cycle:
        raise ValueError(f"선행 관계가 순환합니다: {' → '.join(cycle)}")
    return tasks


# ----- Tool 3: get_free_slots (Planner) ---------------------------------------
def get_free_slots(ctx: PlanContext) -> str:
    if not ctx.tasks:
        return "오류: 등록된 작업이 없습니다. Analyzer가 먼저 submit_tasks를 호출해야 합니다."
    sch = ctx.schedule
    tasks = list(ctx.tasks.values())
    lines = ["[등록된 작업]"] + [_task_line(t) for t in tasks]
    lines += ["", f"[가용 시간표] {sch.rules_text()}"]
    limit = sch.daily_limit(tasks)
    if limit is not None and sch.max_daily_hours is None:
        relaxed = " (마감이 촉박해 권장 시간보다 늘렸습니다)" if limit > (sch.recommended_daily_hours or limit) else ""
        lines.append(f"[하루 배치 한도] 하루에 과제를 {limit:g}h까지만 배치하고, 넘는 분량은 다른 날로 나누세요.{relaxed}")
    for d in sch.days_until(max(t.deadline for t in tasks).date()):
        slots = [(s, e) for s, e in sch.free_slots(d) if e - s >= sch.min_session]
        total = sum(e - s for s, e in slots) / 60
        txt = ", ".join(f"{fmt_minutes(s)}-{fmt_minutes(e)}" for s, e in slots) or "없음"
        cap = "" if sch.max_daily_hours is None else f", 배치 상한 {min(total, sch.max_daily_hours):.1f}h"
        lines.append(f"{fmt_day(d)}: {txt} (빈 시간 {total:.1f}h{cap})")
    lines += ["", "[마감별 필요 시간 vs 배치 가능 시간]"]
    for dl, need, avail in sch.feasibility(tasks):
        mark = "OK" if need <= avail + 1e-6 else f"부족 {need - avail:.1f}h"
        lines.append(f"~{dl:%Y-%m-%d %H:%M}: 필요 {need:.1f}h / 가능 {avail:.1f}h → {mark}")
    if rescheduling.can_resolve_by_postponing(ctx):
        lines.append("→ 시간이 부족하지만, 덜 중요한 다른 과제의 기존 계획을 미루면 해결됩니다. "
                     "postpone_existing_plans 도구를 호출하세요.")
    return "\n".join(lines)


# ----- Tool 4: postpone_existing_plans (Planner) ------------------------------
def postpone_existing_plans(ctx: PlanContext) -> str:
    if not ctx.tasks:
        return "오류: 등록된 작업이 없습니다."
    if rescheduling.current_shortage(ctx) is None:
        return "미룰 필요가 없습니다. 현재 빈 시간만으로 모든 마감을 맞출 수 있습니다. 기존 계획은 그대로 둡니다."
    moved = rescheduling.postpone_existing_plans(ctx)
    if not moved:
        return ("미룰 수 있는 기존 계획이 없습니다. (우선순위가 더 높거나, 미루면 그 과제의 마감을 넘깁니다.) "
                "시간 부족은 해결되지 않았습니다.")
    ctx.just_postponed = True  # 라우터가 Planner를 한 번 더 실행시켜 새 빈 시간에 계획하게 한다
    shortage = rescheduling.current_shortage(ctx)
    status = "시간 부족이 해소되었습니다." if shortage is None else f"아직 {shortage[1]:.1f}h가 부족합니다."
    lines = [f"기존 계획 {len(moved)}개 세션을 뒤로 미뤘습니다. {status} (새 계획이 승인될 때만 확정됩니다)"]
    lines += [f"- {m}" for m in moved]
    return "\n".join(lines) + "\n\n" + get_free_slots(ctx)  # 갱신된 빈 시간표를 함께 돌려준다


# ----- Tool 5: submit_plan (Planner) ------------------------------------------
def submit_plan(ctx: PlanContext, plan_json: str) -> str:
    try:
        data = extract_json(plan_json)
        if isinstance(data, dict):
            data = data.get("sessions", data.get("plan", []))
        if not isinstance(data, list) or not data:
            raise ValueError("세션 목록이 비어 있습니다.")
        sessions = [
            Session(
                str(raw["task_id"]).strip(),
                date.fromisoformat(str(raw["date"]).strip()),
                to_minutes(str(raw["start"])),
                to_minutes(str(raw["end"])),
            )
            for raw in data
        ]
    except (KeyError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return f"오류: 계획 형식이 잘못되었습니다 ({e}). {PLAN_FORMAT} 형식으로 다시 제출하세요."
    sessions = _merge_adjacent(sorted(sessions, key=lambda s: (s.day, s.start)))
    ctx.sessions = sessions
    ctx.plan_pending = True
    per_task: dict[str, float] = {}
    for s in sessions:
        per_task[s.task_id] = per_task.get(s.task_id, 0) + s.hours
    summary = ", ".join(f"{k} {v:.1f}h" for k, v in per_task.items())
    return f"계획 제출 완료: 세션 {len(sessions)}개 ({summary}). Validator의 검증을 기다립니다."


def _merge_adjacent(sessions: list[Session]) -> list[Session]:
    """같은 날 바로 이어지는 같은 작업의 세션은 하나로 합친다. (20:00-22:00 + 22:00-24:00 → 20:00-24:00)"""
    merged: list[Session] = []
    for s in sessions:
        last = merged[-1] if merged else None
        if last and last.task_id == s.task_id and last.day == s.day and last.end == s.start:
            last.end = s.end
        else:
            merged.append(s)
    return merged


# ----- Tool 6: validate_plan (Validator) --------------------------------------
def validate_plan(ctx: PlanContext) -> str:
    """작업 단계와 일정을 함께 검증하고 다음 단계를 정한다.

    판정 순서: 시간 절대 부족(실패) → 작업 단계 문제(Analyzer로 재분해) → 통과 → 재시도 한도(실패) → 반려(Planner)
    """
    ctx.validated_this_round = True
    task_issues = validation.review_tasks(ctx.tasks)
    meal_issues: list[validation.MealIssue] = []
    if ctx.plan_pending:
        errors, warnings = validation.check_plan(ctx)
        meal_issues, meal_warnings = validation.check_meals(ctx)
        warnings += meal_warnings
        # 한 작업을 불필요하게 끊은 경우: '가능하면'의 의미로 첫 검증에서만 반려하고, 그 뒤에는 경고로 남긴다.
        split_notes = validation.check_continuity(ctx)
        (errors if ctx.attempts == 0 else warnings).extend(split_notes)
    else:
        errors, warnings = ["새로 제출된 계획이 없습니다. Planner는 submit_plan 도구로 계획을 제출해야 합니다."], []
    ctx.plan_pending = False
    ctx.pending_meals = meal_issues
    ctx.last_task_issues = task_issues
    ctx.last_errors = errors + [m.describe() for m in meal_issues]
    ctx.last_warnings = warnings
    can_reanalyze = ctx.reanalysis_count < ctx.max_reanalysis
    feasible = ctx.schedule.is_feasible(list(ctx.tasks.values()))
    # 시간이 부족해도, 재시도 기회가 남아 있고 기존 계획을 미루면 해결되는 경우는 반려로 처리한다.
    can_postpone = not feasible and ctx.attempts + 1 < ctx.max_attempts and rescheduling.can_resolve_by_postponing(ctx)

    if not feasible and not can_postpone:  # 작업을 다시 나눠도 해결되지 않으므로 가장 먼저 판정한다
        ctx.attempts += 1
        ctx.status = "failed"
        ctx.failure_reason = "마감 전까지 가용 시간이 부족하여 어떤 계획도 조건을 만족할 수 없음"
        verdict = "FAIL (INFEASIBLE)"
        next_step = "시간이 절대적으로 부족하여 재시도해도 해결되지 않습니다. 실패 보고로 넘어갑니다."
    elif task_issues and can_reanalyze:
        ctx.status = "reanalyze"
        ctx.reanalysis_count += 1
        ctx.reanalysis_reason = "; ".join(task_issues)
        verdict, next_step = "FAIL (작업 단계 재분해 필요)", "Analyzer에게 작업 단계를 다시 나누도록 반려(REANALYZE)하세요."
    else:
        ctx.attempts += 1
        if task_issues:  # 재분해 한도를 넘으면 경고로만 남기고 일정 검증 결과를 따른다
            warnings = warnings + [f"(작업 단계) {x}" for x in task_issues]
            ctx.last_warnings = warnings
        if not errors and not meal_issues:
            ctx.status = "approved"
            verdict, next_step = "PASS", "작업 단계와 일정이 모든 규칙을 통과했습니다. 승인(APPROVE)하세요."
        elif can_postpone:
            verdict = "FAIL (시간 부족, 기존 계획 미루기 필요)"
            next_step = "Planner에게 postpone_existing_plans 도구로 기존 계획을 미룬 뒤 다시 계획하도록 반려(REVISE)하세요."
        elif ctx.attempts >= ctx.max_attempts:
            ctx.status = "failed"
            ctx.failure_reason = f"최대 시도 횟수({ctx.max_attempts}회) 안에 유효한 계획을 만들지 못함"
            verdict, next_step = "FAIL (재시도 한도 초과)", "재시도 한도에 도달했습니다. 실패 보고로 넘어갑니다."
        else:
            verdict, next_step = "FAIL", "Planner에게 위반 사항을 수정하도록 반려(REVISE)하세요."
            if meal_issues:
                next_step = ("식사 시간이 없는 날마다 어느 후보 시간대를 비울지 판단해 reserve_meal 도구로 확정한 뒤, "
                             "그 시간을 비우도록 Planner에게 반려(REVISE)하세요.")

    if ctx.status != "planning":
        ctx.pending_meals = []  # 승인·실패·재분해로 넘어가면 식사 시간을 정할 필요가 없다
    if ctx.reanalysis_pending and ctx.status != "failed":  # 같은 차례에 request_reanalysis가 먼저 호출된 경우
        ctx.status = "reanalyze"

    lines = [f"검증 결과: {verdict} (일정 검증 {ctx.attempts}/{ctx.max_attempts}, "
             f"작업 재분해 {ctx.reanalysis_count}/{ctx.max_reanalysis})"]
    lines.append("[작업 단계 검증] " + ("문제 없음" if not task_issues else f"문제 {len(task_issues)}건"))
    lines += [f"- {x}" for x in task_issues]
    lines.append("[일정 검증] " + ("문제 없음" if not errors else f"위반 {len(errors)}건"))
    lines += [f"{i}. {e}" for i, e in enumerate(errors[:MAX_REPORTED_ERRORS], 1)]
    if len(errors) > MAX_REPORTED_ERRORS:
        lines.append(f"... 외 {len(errors) - MAX_REPORTED_ERRORS}건")
    if ctx.schedule.meals:
        lines.append("[식사 시간] " + ("문제 없음" if not meal_issues else f"확보되지 않은 식사 {len(meal_issues)}건"))
        lines += [f"- {m.describe()}" for m in meal_issues]
    if warnings:
        lines.append("경고(승인에는 영향 없음):")
        lines += [f"- {w}" for w in warnings[:5]]
    lines.append(f"다음 단계: {next_step}")

    # 처음 한 번, 과제 원문 발췌를 보여줘 Validator가 '빠진 제출물'을 대조할 수 있게 한다.
    if can_reanalyze and not ctx.excerpt_shown and ctx.status in ("approved", "planning"):
        ctx.excerpt_shown = True
        try:
            excerpt = ctx.extract_text()[:EXCERPT_CHARS]
        except (OSError, ValueError):
            excerpt = ""
        if excerpt:
            lines += ["", "[작업 단계 대조용] 등록된 작업"] + [_task_line(t) for t in ctx.tasks.values()]
            lines += ["[작업 단계 대조용] 과제 원문 발췌", excerpt,
                      "→ 과제가 요구하는 제출물 중 작업 목록에 빠진 것이 있거나 단계 구성이 잘못됐다면 "
                      "request_reanalysis 도구로 재분해를 요청하세요. 문제가 없으면 호출하지 마세요."]
    return "\n".join(lines)


# ----- Tool 7: request_reanalysis (Validator) ---------------------------------
def request_reanalysis(ctx: PlanContext, reason: str) -> str:
    if not ctx.validated_this_round:
        return ("오류: 먼저 validate_plan 도구로 검증하세요. 재분해 요청은 과제가 요구하는 제출물이 작업 목록에 빠져 있을 때만 "
                "사용합니다. 일정 충돌이나 식사 시간 문제는 재분해가 아니라 Planner 반려(REVISE)로 해결합니다.")
    if ctx.status == "failed":
        return "이미 실패가 확정되어 재분해를 요청할 수 없습니다."
    if ctx.status == "reanalyze":
        return "이미 작업 단계 재분해가 예정되어 있습니다."
    if ctx.reanalysis_count >= ctx.max_reanalysis:
        return f"작업 재분해 한도({ctx.max_reanalysis}회)에 도달해 요청을 반영하지 않습니다. 현재 검증 결과를 따르세요."
    ctx.reanalysis_count += 1
    ctx.reanalysis_pending = True
    ctx.reanalysis_reason = reason.strip()
    ctx.status = "reanalyze"
    return ("작업 단계 재분해 요청을 접수했습니다. 첫 줄에 REANALYZE를 쓰고, Analyzer가 무엇을 어떻게 다시 나눠야 하는지 "
            f"구체적으로 전달하세요. (사유: {ctx.reanalysis_reason})")


# ----- Tool 8: reserve_meal (Validator) ---------------------------------------
def reserve_meal(ctx: PlanContext, date: str, meal: str, start: str) -> str:
    """식사 시간이 없는 날에 비울 시간대를 확정한다. 확정한 시간은 Planner가 쓸 수 없는 시간이 된다."""
    sch = ctx.schedule
    try:
        day = _date.fromisoformat(date.strip())
        begin = to_minutes(start)
    except ValueError as e:
        return f"오류: 날짜(YYYY-MM-DD) 또는 시각(HH:MM) 형식이 잘못되었습니다 ({e})."
    target = next((m for m in sch.meals if m.name == meal.strip().replace(MEAL_SUFFIX.strip(), "").strip()), None)
    if target is None:
        return f"오류: '{meal}'은(는) 알 수 없는 식사입니다. 가능한 값: {', '.join(m.name for m in sch.meals)}"
    end = begin + target.minutes
    if end > 24 * 60:
        return "오류: 식사 시간이 자정을 넘습니다."
    clash = next((n for bs, be, n in sch.busy_intervals(day)
                  if n not in COMMUTE_NAMES and not n.endswith(MEAL_SUFFIX) and begin < be and bs < end), None)
    if clash:
        options = ", ".join(f"{fmt_minutes(s)}{label}" for s, label in validation.meal_candidates(target, sch.occupied_for_meal(day)))
        return f"오류: 그 시간은 '{clash}' 일정과 겹쳐 비울 수 없습니다. 가능한 시작 시각: {options or '없음'}"
    name = target.name + MEAL_SUFFIX
    sch.reserved[day] = [r for r in sch.reserved.get(day, []) if r[2] != name] + [(begin, end, name)]
    ctx.pending_meals = [m for m in ctx.pending_meals if not (m.day == day and m.meal.name == target.name)]
    note = f"{fmt_day(day)} {name} {fmt_minutes(begin)}-{fmt_minutes(end)}"
    ctx.meal_log = [x for x in ctx.meal_log if not x.startswith(f"{fmt_day(day)} {name} ")] + [note]
    left = f" 아직 정하지 않은 식사가 {len(ctx.pending_meals)}건 남았습니다." if ctx.pending_meals else ""
    return (f"식사 시간 확정: {note}. 이 시간에는 과제를 배치할 수 없습니다.{left} "
            "Planner에게 이 시간을 비우고 다시 계획하도록 전달하세요.")


# ----- Tool 9: export_plan (Exporter) -----------------------------------------
def export_plan(ctx: PlanContext) -> str:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if ctx.status == "approved":
        ics = ctx.output_dir / f"study_plan_{stamp}.ics"
        md = ctx.output_dir / f"study_plan_{stamp}.md"
        ics.write_text(export.calendar_ics(ctx), encoding="utf-8")
        md.write_text(export.plan_markdown(ctx), encoding="utf-8")
        ctx.output_files = [ics, md]
        stored = _store_plan(ctx)
        return f"내보내기 완료.\n- 캘린더 파일: {ics}\n- 계획표: {md}{stored}\n\n{export.schedule_text(ctx)}"

    if ctx.status != "failed":
        ctx.status = "failed"
        ctx.failure_reason = ctx.failure_reason or "계획이 승인되기 전에 종료됨"
    report = ctx.output_dir / f"failure_report_{stamp}.md"
    content = export.failure_markdown(ctx)
    report.write_text(content, encoding="utf-8")
    ctx.output_files = [report]
    return f"계획 생성 실패. 실패 보고서: {report}\n\n{content}"


def _store_plan(ctx: PlanContext) -> str:
    """승인된 계획(과 미룬 기존 계획)을 계획 저장소에 반영하고 안내 문구를 반환한다."""
    if ctx.plan_store is None or not ctx.save_plan:
        return ""
    replaced = ctx.plan_store.upsert(
        ctx.assignment_path.name,
        [
            {"task_id": s.task_id, "title": ctx.tasks[s.task_id].title, "date": s.day.isoformat(),
             "start": fmt_minutes(s.start), "end": fmt_minutes(s.end),
             "deadline": ctx.tasks[s.task_id].deadline.strftime("%Y-%m-%d %H:%M")}
            for s in ctx.sessions
        ],
        max(t.deadline for t in ctx.tasks.values()),
        ctx.priority,
    )
    for plan_id, sessions in ctx.postponed.items():
        ctx.plan_store.set_sessions(plan_id, sessions)
    ctx.plan_store.save()
    note = f"\n- 계획 저장소: {ctx.plan_store.path}" + (" (같은 과제의 기존 계획을 교체)" if replaced else "")
    if ctx.postponed_log:
        note += f"\n- 미룬 기존 계획: {len(ctx.postponed_log)}개 세션\n" + "\n".join(f"  · {m}" for m in ctx.postponed_log)
    return note


# ----- AutoGen FunctionTool 등록 ------------------------------------------------
def build_tools(ctx: PlanContext) -> dict[str, FunctionTool]:
    """Agent에 등록할 도구를 만든다. (4주차 실습 1-1, 2-1의 방식)

    수업에서처럼 타입 힌트와 독스트링이 있는 async 함수를 정의하고, FunctionTool로 감싸 이름과 설명을 지정한다.
    함수의 타입 힌트는 도구의 인자 스키마가 되고, 독스트링은 LLM이 읽는 도구 설명이 된다.
    도구는 예외를 던지지 않고 '오류: ...' 문자열을 반환해 LLM이 스스로 대처하게 한다. (4주차 실습 2-2)
    """

    async def read_assignment_tool() -> str:
        """과제 명세 파일(PDF/텍스트)의 내용을 읽어 반환합니다."""
        return read_assignment(ctx)

    async def submit_tasks_tool(tasks_json: str) -> str:
        """분석한 세부 작업 목록(JSON 배열 문자열)을 등록합니다. 형식 오류가 있으면 오류 메시지를 반환합니다."""
        return submit_tasks(ctx, tasks_json)

    async def get_free_slots_tool() -> str:
        """시간표를 바탕으로 계획 기간의 날짜별 빈 시간과 마감별 필요/가용 시간을 계산합니다."""
        return get_free_slots(ctx)

    async def postpone_existing_plans_tool() -> str:
        """시간이 부족할 때, 덜 중요한 다른 과제의 기존 계획을 그 과제의 마감 안에서 뒤로 미뤄 빈 시간을 만듭니다. 갱신된 빈 시간표를 함께 반환합니다."""
        return postpone_existing_plans(ctx)

    async def submit_plan_tool(plan_json: str) -> str:
        """작업 세션 목록(JSON 배열 문자열)을 계획으로 제출합니다."""
        return submit_plan(ctx, plan_json)

    async def validate_plan_tool() -> str:
        """작업 단계(분해 품질)와 제출된 일정(수업·고정 일정·기존 계획과의 충돌, 마감, 작업별 총 시간, 선행 순서, 식사 시간)을 규칙으로 검사합니다."""
        return validate_plan(ctx)

    async def request_reanalysis_tool(reason: str) -> str:
        """과제가 요구하는 제출물이 작업 목록에 빠져 있을 때 Analyzer에게 작업 단계 재분해를 요청합니다. reason에 사유를 적습니다."""
        return request_reanalysis(ctx, reason)

    async def reserve_meal_tool(date: str, meal: str, start: str) -> str:
        """식사 시간이 확보되지 않은 날에 비울 시간대를 확정합니다. date는 YYYY-MM-DD, meal은 '점심' 또는 '저녁', start는 식사 시작 시각 HH:MM입니다. 확정한 시간에는 과제를 배치할 수 없게 됩니다."""
        return reserve_meal(ctx, date, meal, start)

    async def export_plan_tool() -> str:
        """승인된 계획을 .ics 캘린더와 마크다운 계획표로 저장합니다. 실패 상태이면 실패 보고서를 저장합니다."""
        return export_plan(ctx)

    functions = {
        "read_assignment": read_assignment_tool,
        "submit_tasks": submit_tasks_tool,
        "get_free_slots": get_free_slots_tool,
        "postpone_existing_plans": postpone_existing_plans_tool,
        "submit_plan": submit_plan_tool,
        "validate_plan": validate_plan_tool,
        "request_reanalysis": request_reanalysis_tool,
        "reserve_meal": reserve_meal_tool,
        "export_plan": export_plan_tool,
    }
    return {name: FunctionTool(func=func, name=name, description=func.__doc__) for name, func in functions.items()}
