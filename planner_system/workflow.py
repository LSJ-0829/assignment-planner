"""GraphFlow 구성, 라우팅(엣지 조건), Termination Condition.

    ┌──────────(작업 단계 문제 → 재분해)───────────┐
    ▼                                              │
analyzer ──(작업 등록 성공)──► planner ──► validator ──(PASS / 실패 확정)──► exporter
    │                           ▲ ↺ ▲           │
    └──(작업 등록 실패)──► exporter  │ └──(일정 문제 → 재배치)─┘
                                  └ 기존 계획을 미룬 직후 한 번 더 실행

다음 Agent는 LLM이 쓴 'APPROVE' 같은 문자열이 아니라, 도구가 PlanContext에 기록한 상태로 결정한다.
"""
import json

from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination, TimeoutTermination
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow
from autogen_core.models import ChatCompletionClient

from . import tools
from .agents import build_agents
from .context import PlanContext
from .utils import extract_json

MAX_MESSAGES = 30
TIMEOUT_SECONDS = 600


class Router:
    """GraphFlow 엣지 조건 함수 모음.

    GraphFlow는 한 메시지에 대해 나가는 엣지마다 조건을 호출하므로, 판정은 메시지 ID별로 한 번만 하고 캐시한다.
    Agent가 도구 호출을 빠뜨리는 전달 오류도 여기서 보정한다.
    """

    def __init__(self, ctx: PlanContext):
        self.ctx = ctx
        self._decisions: dict[tuple[str, str], str] = {}

    def _once(self, kind: str, message, decide) -> str:
        key = (kind, str(getattr(message, "id", id(message))))
        if key not in self._decisions:
            self._decisions[key] = decide()
        return self._decisions[key]

    def after_analyzer(self, message) -> str:
        def decide() -> str:
            if self.ctx.tasks:
                self.ctx.log("router: analyzer → planner")
                return "plan"
            self.ctx.status = "failed"
            self.ctx.failure_reason = self.ctx.failure_reason or "Analyzer가 작업 목록을 등록하지 못함"
            self.ctx.log("router: analyzer → exporter (실패 보고)")
            return "export"

        return self._once("analyzer", message, decide)

    def after_planner(self, message) -> str:
        """'validate' 또는 'replan'.

        - 기존 계획을 미루느라 도구 호출 한도를 써서 계획을 제출하지 못했으면 Planner를 한 번 더 실행한다.
        - Planner가 도구 대신 텍스트로 계획 JSON을 보낸 경우 시스템이 대신 제출한다.
        """

        def decide() -> str:
            postponed, self.ctx.just_postponed = self.ctx.just_postponed, False
            if postponed and not self.ctx.plan_pending:
                self.ctx.log("router: planner → planner (기존 계획을 미뤘으므로 새 빈 시간으로 다시 계획)")
                return "replan"
            content = getattr(message, "content", "")
            if self.ctx.plan_pending or not isinstance(content, str) or "task_id" not in content:
                return "validate"
            try:
                data = extract_json(content)
                if isinstance(data, dict) and "plan_json" in data:
                    data = data["plan_json"]
                payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                result = tools.submit_plan(self.ctx, payload)
                self.ctx.log(f"router: Planner가 도구 대신 텍스트로 계획을 보내 시스템이 대신 제출 → {result[:40]}")
            except (ValueError, json.JSONDecodeError):
                pass
            return "validate"

        return self._once("planner", message, decide)

    def after_validator(self, message) -> str:
        def decide() -> str:
            if not self.ctx.validated_this_round:
                self.ctx.log("router: Validator가 validate_plan을 호출하지 않아 시스템이 직접 검증합니다.")
                tools.validate_plan(self.ctx)
            self.ctx.validated_this_round = False
            self.ctx.reanalysis_pending = False
            # Validator가 비울 식사 시간대를 정하지 않았으면, 가장 권장되는 후보로 시스템이 대신 확정한다.
            for issue in list(self.ctx.pending_meals):
                start = issue.candidates[0][0]
                tools.reserve_meal(self.ctx, issue.day.isoformat(), issue.meal.name, f"{start // 60:02d}:{start % 60:02d}")
                self.ctx.log(f"router: Validator가 {issue.day:%m/%d} {issue.meal.name} 시간을 정하지 않아 "
                             f"{start // 60:02d}:{start % 60:02d}로 확정합니다.")
            if self.ctx.status in ("approved", "failed"):
                self.ctx.log("router: validator → exporter")
                return "export"
            if self.ctx.status == "reanalyze":
                self.ctx.status = "planning"  # Analyzer가 다시 제출하지 못해도 기존 작업으로 계속 진행한다
                self.ctx.log(f"router: validator → analyzer (작업 단계 재분해: {self.ctx.reanalysis_reason[:60]})")
                return "reanalyze"
            self.ctx.log("router: validator → planner (반려)")
            return "revise"

        return self._once("validator", message, decide)


def build_termination():
    """종료 조건. (4주차 실습 3-2의 OR 조합 '안전 장치 패턴')

    정상 종료: Exporter가 완료(PLAN_COMPLETE) 또는 실패(PLAN_FAILED)를 선언
    안전 종료: 메시지 수 상한, 시간 제한 — 무한 반복 방지
    """
    termination = (
        TextMentionTermination("PLAN_COMPLETE", sources=["exporter"])
        | TextMentionTermination("PLAN_FAILED", sources=["exporter"])
        | MaxMessageTermination(MAX_MESSAGES)
        | TimeoutTermination(timeout_seconds=TIMEOUT_SECONDS)
    )
    return termination


def build_flow(ctx: PlanContext, model_client: ChatCompletionClient) -> GraphFlow:
    """GraphFlow 구성. (3주차 실습 7-1 '조건 분기 + 루프', 7-3 '조건 분기'의 방식)

    수업의 Writer → Reviewer → (APPROVE이면 Publisher / 아니면 Writer) 구조를 확장했다.
    차이점: 엣지 조건이 메시지 문자열("APPROVE" in msg.to_model_text())이 아니라 도구가 기록한 상태를 본다.
    """
    agents = build_agents(ctx, model_client)
    analyzer, planner, validator, exporter = (agents[k] for k in ("analyzer", "planner", "validator", "exporter"))
    router = Router(ctx)

    # 그래프 구성
    builder = DiGraphBuilder()
    builder.add_node(analyzer).add_node(planner).add_node(validator).add_node(exporter)
    builder.set_entry_point(analyzer)

    # Analyzer → Planner (작업 등록 성공) / Exporter (등록 실패 → 실패 보고)
    # 들어오는 엣지가 여러 개인 노드는 activation_group을 나눠, 하나만 도착해도 실행되게 한다.
    builder.add_edge(analyzer, planner, condition=lambda msg: router.after_analyzer(msg) == "plan",
                     activation_group="initial")
    builder.add_edge(analyzer, exporter, condition=lambda msg: router.after_analyzer(msg) == "export",
                     activation_group="from_analyzer")

    # Planner → Validator / Planner (기존 계획을 미룬 직후 한 번 더)
    builder.add_edge(planner, validator, condition=lambda msg: router.after_planner(msg) == "validate")
    builder.add_edge(planner, planner, condition=lambda msg: router.after_planner(msg) == "replan",
                     activation_group="replan")

    # 조건 분기 + 루프: Validator → Planner (일정 반려) / Analyzer (작업 재분해) / Exporter (승인 또는 실패 확정)
    builder.add_edge(validator, planner, condition=lambda msg: router.after_validator(msg) == "revise",
                     activation_group="feedback")
    builder.add_edge(validator, analyzer, condition=lambda msg: router.after_validator(msg) == "reanalyze",
                     activation_group="reanalysis")
    builder.add_edge(validator, exporter, condition=lambda msg: router.after_validator(msg) == "export",
                     activation_group="from_validator")

    graph = builder.build()
    flow = GraphFlow(
        participants=builder.get_participants(),
        graph=graph,
        termination_condition=build_termination(),
    )
    return flow
