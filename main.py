"""과제 수행 계획 Multi Agent System 실행 진입점 (터미널).

예)  python main.py --app                             # 화면을 앱 창으로 실행 (과제 PDF 끌어다 놓기)
    python main.py                                   # 과제1 명세서로 지금부터 계획 (터미널)
    python main.py --assignment data/hw3_no_date.pdf --deadline 10/1 --note "전기자기학1 과제, 서술형 2문제"
    python main.py --add-weekly "화 18:00-21:45 인공지능프로젝트" --repeat 16    # 매주 반복 일정 추가
    python main.py --add-event "10/9 18:00-20:00 저녁 약속"                    # 특정 날짜의 약속 추가
    python main.py --week                            # 이번 주 시간표 보기
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from planner_system import config, rescheduling
from planner_system.context import PlanContext
from planner_system.display import print_event, print_trace
from planner_system.event_store import EventStore, parse_event
from planner_system.plan_store import PRIORITIES, PlanStore
from planner_system.runner import missing_inputs, run_planning
from planner_system.timetable_store import TimetableStore, parse_weekly
from planner_system.utils import parse_user_deadline
from planner_system.week_view import show_week

BASE = Path(__file__).resolve().parent
EXIT_OK, EXIT_INPUT_ERROR, EXIT_PLAN_FAILED = 0, 1, 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="과제 명세 + 시간표 → 검증된 과제 수행 계획(.ics)")
    g = p.add_argument_group("계획 세우기")
    g.add_argument("--assignment", help="과제 명세 파일 (.pdf/.txt/.md). 생략하면 data/assignment1.pdf가 있을 때 그것을 사용")
    g.add_argument("--start", help='계획 시작 시점 "YYYY-MM-DD HH:MM" (기본: 현재 시각)')
    g.add_argument("--deadline", help='최종 마감 (예: "2026-10-01 23:59", "10/1"). 문서에 날짜가 없으면 생략 시 물어봄')
    g.add_argument("--note", default="", help="과제 설명 (문서가 이미지라 내용을 읽을 수 없을 때)")
    g.add_argument("--priority", choices=PRIORITIES, default="normal",
                   help="이 과제의 우선순위. 시간이 부족하면 우선순위가 낮거나 같은 다른 과제의 계획을 뒤로 미룸")
    g.add_argument("--max-attempts", type=int, default=3, help="일정 검증 최대 시도 횟수")
    g.add_argument("--max-reanalysis", type=int, default=1, help="작업 단계 재분해(Analyzer 반려) 최대 횟수")
    g.add_argument("--no-save", action="store_true", help="승인된 계획을 저장소에 저장하지 않음 (시험 실행)")

    g = p.add_argument_group("실행 과정 보기 (수업 실습 방식)")
    g.add_argument("--console", action="store_true", help="AutoGen Console로 전체 메시지와 도구 결과를 그대로 출력")
    g.add_argument("--trace", action="store_true", help="실행 후 대화 흐름과 메시지별 토큰 사용량 분석을 출력")
    g.add_argument("--debug", action="store_true", help="AutoGen 내부 로그(Agent 선택, 도구 호출)를 출력")

    g = p.add_argument_group("데이터 파일")
    g.add_argument("--timetable", default=str(BASE / "data" / "timetable.json"),
                   help="시간표 JSON 파일 (매주 반복 일정). 없으면 빈 시간표로 시작")
    g.add_argument("--events", default=str(BASE / "data" / "events.json"), help="일회성 약속 저장소")
    g.add_argument("--plans", default=str(BASE / "data" / "plans.json"), help="확정된 계획 저장소")
    g.add_argument("--output", default=str(BASE / "outputs"), help="결과 저장 폴더")

    g = p.add_argument_group("API가 필요 없는 명령")
    g.add_argument("--add-weekly", metavar='"요일 시작-종료 이름"',
                   help='매주 반복되는 일정 추가. 예: "화 18:00-21:45 인공지능프로젝트"')
    g.add_argument("--repeat", type=int, metavar="횟수", help="--add-weekly: 반복 횟수 (예: 16주 수업이면 16). 생략하면 계속 반복")
    g.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="--add-weekly: 반복을 시작하는 날짜 (기본: 오늘)")
    g.add_argument("--short", default="", help="--add-weekly: 주간 시간표에 표시할 약어")
    g.add_argument("--off-campus", action="store_true", help="--add-weekly: 학교 밖 일정 (등교·귀가 이동 시간을 붙이지 않음)")
    g.add_argument("--list-weekly", action="store_true", help="매주 반복 일정 목록")
    g.add_argument("--remove-weekly", metavar="ID", help="매주 반복 일정 삭제")
    g.add_argument("--add-event", metavar='"날짜 시작-종료 이름"',
                   help='특정 날짜의 약속 추가. 예: "10/9 18:00-20:00 저녁 약속". 겹치는 과제 계획은 자동으로 옮김')
    g.add_argument("--list-events", action="store_true", help="등록된 약속 목록")
    g.add_argument("--remove-event", metavar="ID", help="약속 삭제")
    g.add_argument("--list-plans", action="store_true", help="저장된 계획 목록")
    g.add_argument("--remove-plan", metavar="ID_또는_파일명", help="저장된 계획 삭제")
    g.add_argument("--week", nargs="?", const="today", metavar="YYYY-MM-DD", help="해당 날짜(기본: 오늘)가 속한 주의 시간표")
    g = p.add_argument_group("화면(GUI)")
    g.add_argument("--app", action="store_true", help="화면을 독립된 앱 창으로 실행 (과제 PDF를 끌어다 놓아 계획)")
    g.add_argument("--web", nargs="?", const=8000, type=int, metavar="PORT", help="같은 화면을 브라우저 주소로 실행 (기본 포트 8000)")
    return p.parse_args()


def load_timetable(args: argparse.Namespace, events: EventStore) -> dict:
    """매주 반복되는 시간표(없으면 빈 시간표)에 일회성 약속을 합친다."""
    return events.merged_into(TimetableStore(args.timetable).data)


def report_moved_plans(plans: PlanStore, timetable: dict, now: datetime) -> None:
    """새 일정과 겹치게 된 기존 과제 계획을 옮기고 알려 준다."""
    moved, unresolved = rescheduling.repair_conflicts(plans, timetable, now)
    for line in moved:
        print(f"  겹치는 과제 계획을 옮겼습니다: {line}")
    for line in unresolved:
        print(f"  [주의] {line}")


# ----- API가 필요 없는 명령 ------------------------------------------------------
def run_offline_command(args: argparse.Namespace, plans: PlanStore, events: EventStore) -> int | None:
    """약속·계획 관리, 주간 시간표, 웹 화면. 해당 명령이 아니면 None."""
    now = datetime.now().replace(second=0, microsecond=0)
    if args.app or args.web:
        from planner_system.webapp import serve, serve_app

        ensure_api_key()
        paths = dict(timetable=args.timetable, events=args.events, plans=args.plans, output=args.output)
        serve_app(BASE, **paths) if args.app else serve(BASE, port=args.web, **paths)
        return EXIT_OK
    if args.add_weekly:
        timetable = TimetableStore(args.timetable)
        entry = timetable.add_weekly(count=args.repeat, start_date=args.from_date, short=args.short,
                                     at_school=not args.off_campus, **parse_weekly(args.add_weekly))
        timetable.save()
        info = timetable.describe(entry, now.date())
        repeat = "계속 반복" if info["count"] is None else f"{info['count']}회 반복 (~{info['last_date']})"
        print(f"매주 반복 일정을 추가했습니다: {info['weekday']} {info['start']}-{info['end']} {info['name']} · "
              f"{info['from']}부터 {repeat}")
        report_moved_plans(plans, load_timetable(args, events), now)
        return EXIT_OK
    if args.remove_weekly:
        timetable = TimetableStore(args.timetable)
        removed = timetable.remove_weekly(args.remove_weekly)
        if not removed:
            print(f"'{args.remove_weekly}'에 해당하는 반복 일정이 없습니다.\n{timetable.summary(now.date())}")
            return EXIT_INPUT_ERROR
        timetable.save()
        print(f"삭제했습니다: {removed['day']} {removed['start']}-{removed['end']} {removed['name']}")
        return EXIT_OK
    if args.list_weekly:
        print(TimetableStore(args.timetable).summary(now.date()))
        return EXIT_OK
    if args.add_event:
        event = events.add(**parse_event(args.add_event, now))
        events.save()
        print(f"약속을 추가했습니다: {event['date']} {event['start']}-{event['end']} {event['name']}")
        report_moved_plans(plans, load_timetable(args, events), now)
        return EXIT_OK
    if args.remove_event:
        removed = events.remove(args.remove_event)
        if not removed:
            print(f"'{args.remove_event}'에 해당하는 약속이 없습니다.\n{events.summary(now.date())}")
            return EXIT_INPUT_ERROR
        events.save()
        print(f"삭제했습니다: {removed['date']} {removed['start']}-{removed['end']} {removed['name']}")
        return EXIT_OK
    if args.list_events:
        print(events.summary(now.date()))
        return EXIT_OK
    if args.list_plans:
        print(plans.summary())
        return EXIT_OK
    if args.remove_plan:
        removed = plans.remove(args.remove_plan)
        if not removed:
            print(f"'{args.remove_plan}'에 해당하는 계획이 없습니다.\n{plans.summary()}")
            return EXIT_INPUT_ERROR
        plans.save()
        print(f"삭제했습니다: {removed['id']} {removed['assignment']}\n{plans.summary()}")
        return EXIT_OK
    if args.week:
        target = date.today() if args.week == "today" else date.fromisoformat(args.week)
        path = show_week(load_timetable(args, events), plans, target, Path(args.output))
        print(f"\nHTML로 저장했습니다 (브라우저로 열기): {path}")
        return EXIT_OK
    return None


# ----- 계획 세우기 ---------------------------------------------------------------
def ensure_api_key() -> bool:
    """API 키가 없으면 처음 한 번 입력받아 code/.env에 저장한다. (설치 과정을 줄이기 위한 안내)"""
    if config.has_api_key():
        return True
    print("Groq API 키가 설정되어 있지 않습니다. https://console.groq.com/keys 에서 무료로 발급받을 수 있습니다.")
    key = ask("Groq API 키를 붙여 넣으세요 (나중에 하려면 Enter): ")
    if not key:
        print("키 없이 계속합니다. 시간표·약속·저장된 계획 보기는 쓸 수 있지만, 계획 세우기에는 키가 필요합니다.")
        return False
    print(f"키를 저장했습니다: {config.save_api_key(key)}  (이 파일은 저장소와 제출물에 포함되지 않습니다)")
    return True


def ask(question: str) -> str:
    """터미널에서 실행 중일 때만 사용자에게 입력을 받는다."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(question).strip()
    except EOFError:
        return ""


def create_context(args: argparse.Namespace, plans: PlanStore, events: EventStore) -> PlanContext:
    if not args.assignment:
        default = BASE / "data" / "assignment1.pdf"
        if not default.exists():
            raise ValueError("계획할 과제 파일을 지정하세요. 예: python main.py --assignment 과제.pdf\n"
                             "화면에서 파일을 끌어다 놓으려면: python main.py --app")
        args.assignment = str(default)
    start = (datetime.strptime(args.start, "%Y-%m-%d %H:%M") if args.start
             else datetime.now().replace(second=0, microsecond=0))
    ctx = PlanContext(args.assignment, load_timetable(args, events), start, output_dir=args.output,
                      max_attempts=args.max_attempts, plan_store=plans, save_plan=not args.no_save,
                      priority=args.priority, max_reanalysis=args.max_reanalysis)
    existing = sum(len(v) for v in ctx.schedule.existing.values())
    if existing:
        print(f"[system] 다른 과제의 기존 계획 {existing}개 세션을 이미 찬 시간으로 반영합니다.")
    if plans.find(ctx.assignment_path.name):
        print(f"[system] '{ctx.assignment_path.name}'의 저장된 계획이 있어, 승인되면 새 계획으로 교체합니다.")
    return ctx


def collect_user_inputs(ctx: PlanContext, args: argparse.Namespace) -> str | None:
    """문서에 없는 정보(마감, 과제 설명)를 옵션이나 입력으로 보충한다. 진행할 수 없으면 오류 메시지를 반환."""
    ctx.user_note = args.note.strip()
    if args.deadline:
        try:
            ctx.user_deadline = parse_user_deadline(args.deadline, ctx.start)
        except ValueError as e:
            return str(e)
    missing = missing_inputs(ctx)
    if "note" in missing:
        print("과제 문서에서 텍스트를 거의 읽지 못했습니다. (문제가 이미지로 된 PDF일 수 있음)")
        ctx.user_note = ask("과제 내용을 한 줄로 설명해 주세요 (예: 전기자기학 과제, 서술형 2문제): ")
        if not ctx.user_note:
            return "과제 내용을 알 수 없습니다. --note 옵션으로 과제 설명을 입력하세요."
    if "deadline" in missing:
        print("과제 문서에서 마감일을 찾지 못했습니다.")
        while ctx.user_deadline is None:
            answer = ask("마감일을 입력하세요 (예: 2026-10-01 23:59, 10/1, 10월 1일): ")
            if not answer:
                return "마감일이 필요합니다. --deadline 옵션으로 마감일을 입력하세요."
            try:
                ctx.user_deadline = parse_user_deadline(answer, ctx.start)
            except ValueError as e:
                print(e)
    if ctx.user_deadline:
        print(f"[system] 사용자 입력 마감: {ctx.user_deadline:%Y-%m-%d %H:%M}")
    return None


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 60 + "\n실행 요약\n" + "=" * 60)
    if summary["stop_reason"]:
        print(f"종료 사유   : {summary['stop_reason']}")
        print(f"메시지 수   : {summary['messages']}")
        print(f"토큰 사용량 : 입력 {summary['tokens_in']:,} / 출력 {summary['tokens_out']:,}")
    print(f"최종 상태   : {summary['status']}" + (f" ({summary['failure_reason']})" if summary["failure_reason"] else ""))
    print(f"일정 검증   : {summary['attempts']}회 / 최대 {summary['max_attempts']}회")
    print(f"작업 재분해 : {summary['reanalysis']}회 / 최대 {summary['max_reanalysis']}회")
    if summary["postponed"]:
        print(f"미룬 일정   : 기존 계획 {len(summary['postponed'])}개 세션")
    for f in summary["files"]:
        print(f"결과 파일   : {f}")


def main() -> int:
    args = parse_args()
    if args.debug:  # 4주차 실습 5-3: AutoGen 내부 로그 활성화
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("autogen_agentchat").setLevel(logging.DEBUG)
    try:
        plans, events = PlanStore(args.plans), EventStore(args.events)
        offline = run_offline_command(args, plans, events)
        if offline is not None:
            return offline
        ctx = create_context(args, plans, events)
    except (OSError, ValueError) as e:
        print(f"입력 오류: {e}")
        return EXIT_INPUT_ERROR

    error = collect_user_inputs(ctx, args)
    if error:
        print(f"입력 오류: {error}")
        return EXIT_INPUT_ERROR

    if not ensure_api_key():
        print("입력 오류: 계획을 세우려면 Groq API 키가 필요합니다. (.env의 GROQ_API_KEY)")
        return EXIT_INPUT_ERROR
    summary = asyncio.run(run_planning(ctx, print_event, raw_console=args.console))
    if args.trace:
        print()
        print_trace(summary["trace"])
    print_summary(summary)
    if summary["status"] != "approved":
        return EXIT_PLAN_FAILED
    print()
    path = show_week(load_timetable(args, events), plans, date.today(), Path(args.output))
    print(f"\n이번 주 시간표 HTML: {path}")
    return EXIT_OK


if __name__ == "__main__":
    # AutoGen 런타임 내부 traceback 출력을 숨긴다. 오류 요약은 한 줄로 출력한다. (--debug이면 그대로 둔다)
    if "--debug" not in sys.argv:
        logging.getLogger("autogen_core").setLevel(logging.CRITICAL)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
