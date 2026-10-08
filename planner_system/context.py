"""Tool들이 공유하는 작업 상태(PlanContext).

Tool 함수는 tools.py에, 검증 규칙은 validation.py에, 기존 계획 미루기는 rescheduling.py에 있다.
이 모듈은 그 사이에서 오가는 상태와 과제 파일 읽기만 담당한다.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from .plan_store import PlanStore, SessionRecord
from .schedule import Schedule, Session, Task
from .utils import to_minutes

MIN_ASSIGNMENT_CHARS = 20  # 이보다 적으면 이미지 PDF 등으로 보고 사용자 설명을 요구


class PlanContext:
    """한 번의 실행(과제 하나의 계획 수립) 동안 유지되는 상태.

    status: analyzing → planning(작업 등록됨) → approved(검증 통과) | failed(실패 확정)
            검증에서 작업 단계 문제가 발견되면 reanalyze(Analyzer로 반려)를 거쳐 다시 planning이 된다.
    """

    def __init__(
        self,
        assignment_path: str | Path,
        timetable: dict,
        start: datetime,
        output_dir: str | Path = "outputs",
        max_attempts: int = 3,
        verbose: bool = True,
        plan_store: PlanStore | None = None,
        save_plan: bool = True,
        priority: str = "normal",
        max_reanalysis: int = 1,
    ):
        self.assignment_path = Path(assignment_path)
        self.start = start
        self.output_dir = Path(output_dir)
        self.max_attempts = max_attempts  # 일정 검증 최대 시도 횟수
        self.max_reanalysis = max_reanalysis  # 작업 단계 재분해(Analyzer 반려) 최대 횟수
        self.verbose = verbose
        self.plan_store = plan_store
        self.save_plan = save_plan
        self.priority = priority  # 이 과제의 우선순위 (low / normal / high)

        # 다른 과제 때문에 미룬 기존 계획: {계획 ID: 새 세션 목록}. 승인될 때만 저장소에 반영한다.
        self.postponed: dict[str, list[SessionRecord]] = {}
        self.postponed_log: list[str] = []
        self.just_postponed = False  # 이번 Planner 차례에 기존 계획을 미뤘는가
        self.schedule = Schedule(timetable, start)
        self.refresh_existing()

        # 사용자가 직접 보충한 정보 (문서에 없을 때)
        self.user_deadline: datetime | None = None
        self.user_note = ""

        # 워크플로 진행 상태
        self.tasks: dict[str, Task] = {}
        self.sessions: list[Session] = []
        self.plan_pending = False  # 제출됐지만 아직 검증되지 않은 계획이 있는가
        self.validated_this_round = False  # 이번 Validator 차례에 validate_plan이 호출됐는가
        self.attempts = 0  # 일정 검증 횟수
        self.reanalysis_count = 0  # 작업 단계 재분해 횟수
        self.reanalysis_pending = False  # Validator가 request_reanalysis로 재분해를 요청했는가
        self.reanalysis_reason = ""
        self.excerpt_shown = False  # Validator에게 과제 원문 발췌를 이미 보여줬는가
        self.last_errors: list[str] = []
        self.last_warnings: list[str] = []
        self.last_task_issues: list[str] = []
        self.pending_meals: list = []  # 식사 시간이 없어 비울 시간대를 정해야 하는 날 (validation.MealIssue)
        self.meal_log: list[str] = []  # 확정한 식사 시간
        self.status = "analyzing"
        self.failure_reason = ""
        self.output_files: list[Path] = []

        self._text: str | None = None
        self.defer_logs = False
        self.pending_logs: list[str] = []

    # ----- 기존 계획(다른 과제) --------------------------------------------------
    def refresh_existing(self) -> None:
        """계획 저장소(+ 미룬 일정)를 다시 읽어 '이미 찬 시간'을 갱신한다."""
        existing = {}
        if self.plan_store is not None:
            busy = self.plan_store.busy_by_date(self.assignment_path.name, self.start.date(), self.postponed)
            for d, items in busy.items():
                existing[d] = [(to_minutes(s), to_minutes(e), name) for s, e, name in items]
        self.schedule.existing = existing

    def reset_plan(self) -> None:
        """작업 목록이 바뀌면 이전 계획과 미룬 일정을 버린다."""
        self.sessions = []
        self.plan_pending = False
        self.postponed.clear()
        self.postponed_log.clear()
        self.schedule.reserved.clear()
        self.pending_meals = []
        self.meal_log = []
        self.refresh_existing()

    # ----- 시스템 로그 --------------------------------------------------------
    def log(self, msg: str) -> None:
        if not self.verbose:
            return
        if self.defer_logs:  # 화면 표시기가 Agent 출력 순서에 맞춰 출력한다
            self.pending_logs.append(msg)
        else:
            print(f"\033[90m[system] {msg}\033[0m", flush=True)

    def flush_logs(self) -> None:
        for msg in self.pending_logs:
            print(f"\033[90m[system] {msg}\033[0m", flush=True)
        self.pending_logs.clear()

    # ----- 과제 파일 ----------------------------------------------------------
    def extract_text(self) -> str:
        """과제 파일의 텍스트. 파일이 없으면 OSError, 읽지 못하면 ValueError."""
        if self._text is None:
            path = self.assignment_path
            if not path.exists():
                raise OSError(f"과제 파일을 찾을 수 없음: {path}")
            try:
                if path.suffix.lower() == ".pdf":
                    from pypdf import PdfReader

                    logging.getLogger("pypdf").setLevel(logging.ERROR)
                    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
                else:
                    text = path.read_text(encoding="utf-8")
            except Exception as e:  # 손상된 파일 등
                raise ValueError(f"과제 파일 읽기 실패: {e}") from e
            text = re.sub(r"[ \t]+", " ", text)
            self._text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return self._text

    def text_is_sparse(self) -> bool:
        """텍스트가 거의 없는 파일(문제가 이미지로 된 PDF 등)인가."""
        try:
            return len(re.sub(r"\s", "", self.extract_text())) < MIN_ASSIGNMENT_CHARS
        except (OSError, ValueError):
            return False
