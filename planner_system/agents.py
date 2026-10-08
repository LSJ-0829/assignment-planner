"""4개 Agent 정의: Analyzer, Planner, Validator, Exporter. (Tool 구현은 tools.py)"""
from typing import Callable

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import TextMessage
from autogen_core.models import ChatCompletionClient

from . import tools as tool_functions
from .context import PlanContext
from .tools import build_tools
from .validation import MAX_TASK_HOURS


class GuardedAssistantAgent(AssistantAgent):
    """반드시 거쳐야 하는 도구 단계를 LLM이 건너뛰어도 결과가 왜곡되지 않게 하는 AssistantAgent.

    LLM의 응답이 끝난 뒤 guard()를 호출한다. guard가 문자열을 돌려주면(= 필수 단계를 건너뛰었으면)
    LLM의 응답 대신 그 문자열을 이 Agent의 발화로 내보낸다. 다른 Agent는 실제 도구 결과만 보게 된다.
    (실제 실행에서 Validator가 검증 도구 없이 'APPROVE'라고 답해 Planner가 반려 사유를 받지 못한 문제의 대응)
    """

    def __init__(self, *args, guard: Callable[[], str | None], **kwargs):
        super().__init__(*args, **kwargs)
        self._guard = guard

    async def on_messages_stream(self, messages, cancellation_token):
        async for item in super().on_messages_stream(messages, cancellation_token):
            if isinstance(item, Response):
                correction = self._guard()
                if correction is not None:
                    item = Response(chat_message=TextMessage(content=correction, source=self.name),
                                    inner_messages=item.inner_messages)
            yield item

ANALYZER_PROMPT = f"""당신은 대학생의 과제 명세서를 분석하는 Analyzer입니다.
모든 응답은 한국어로 작성하세요.
1. 먼저 read_assignment 도구로 과제 파일을 읽으세요.
2. 과제를 실제로 해야 하는 세부 작업 3~8개로 나누세요. (예: 주제 선정, 코드 구현, 리포트 작성, 발표 자료, 시연 영상, 최종 제출)
   - 과제가 요구하는 제출물이 하나도 빠지지 않게 하세요.
   - 한 작업은 {MAX_TASK_HOURS}시간을 넘지 않게 하세요. 넘으면 진행 단계별로 더 나눕니다. (예: 코드 구현 → 핵심 기능 구현 / 예외 처리·테스트)
   - 과제 문서가 영어여도 작업 이름(title)은 한국어로 적으세요.
3. 각 작업에 대학생 기준의 현실적인 예상 소요 시간(hours)을 정하세요. 0.5 단위로 적습니다.
4. 각 작업의 마감(deadline)은 과제 문서에 명시된 날짜만 사용하세요. 문서에 없는 중간 마감을 임의로 만들지 마세요.
   - 중간 발표처럼 별도 날짜가 있는 작업은 그 날짜의 시작 전(예: 발표 당일 09:00)으로 정합니다.
   - 다른 작업의 선행 작업이면, 그 작업과 같은 마감을 사용합니다.
   - 그 외의 작업은 최종 마감을 사용합니다. 연도가 없으면 [계획 시작 시점]의 연도를 사용하세요.
   - [사용자 입력 마감]이 있으면 그것을 최종 마감으로 사용하고, 어떤 작업도 그보다 늦은 마감을 가질 수 없습니다.
   - [사용자 설명]이 있으면 과제 내용을 파악할 때 함께 참고하세요. 문서가 이미지라 내용이 거의 없으면 설명과 문제 번호를 기준으로 작업을 나눕니다.
5. 앞 작업의 결과가 필요한 작업에는 depends_on에 선행 작업 ID를 적으세요.
6. submit_tasks 도구에 JSON 배열 문자열로 제출하세요:
   [{{"id":"T1","title":"작업명","hours":2,"deadline":"YYYY-MM-DD HH:MM","depends_on":[]}}]
   도구가 오류를 반환하면 내용을 고쳐 다시 제출하세요.
7. 제출이 끝나면 등록된 작업을 3~5줄로 요약하세요. 일정 배치는 하지 마세요(Planner의 역할입니다).
8. Validator가 REANALYZE와 함께 작업 단계의 문제를 지적했다면, read_assignment를 다시 호출하지 말고
   지적된 부분을 반영해 작업을 다시 나눈 '전체' 목록을 submit_tasks로 다시 제출하세요.
과제 파일을 읽을 수 없으면 억지로 작업을 만들지 말고 오류 내용을 보고하세요."""

PLANNER_PROMPT = """당신은 학생의 빈 시간에 과제 작업을 배치하는 Planner입니다.
모든 응답은 한국어로 작성하세요.
1. 먼저 get_free_slots 도구로 등록된 작업, 날짜별 빈 시간, 마감별 가용 시간을 확인하세요.
2. 시간이 '부족'으로 표시되고 도구가 postpone_existing_plans를 안내하면, 그 도구를 호출해 덜 중요한 다른 과제의
   기존 계획을 뒤로 미루세요. 도구가 갱신된 빈 시간표를 돌려주며, 그 시간표로 이어서 계획하면 됩니다.
   부족하지 않으면 호출하지 마세요.
3. 다음 규칙을 모두 지키는 세션 목록을 만드세요.
   - 세션은 반드시 도구가 알려준 빈 시간 범위 안에만 둡니다. (수업·고정 일정·이동 시간·다른 과제 계획과 겹치면 안 됨)
   - '배치 상한'이 표시된 날은 하루 세션 합계가 그 값을 넘지 않습니다.
   - 각 작업의 세션 합계는 작업의 예상 시간 이상이어야 합니다.
   - 모든 세션은 해당 작업의 마감 전에 끝나야 합니다.
   - 선행 작업(depends_on)이 먼저 시작하고 먼저 끝나야 합니다. 반려 사항을 고칠 때도 이 순서를 지키세요.
   - 한 작업은 가능하면 같은 날에 이어서 배치합니다. 빈 시간표에는 식사 시간과 수업이 이미 빠져 있으므로,
     작업이 그 사이에 걸치면 앞뒤 빈 시간에 이어서 배치하면 됩니다. (예: 10:00-12:00, 13:00-16:00)
     하루 안에 다 들어가지 않을 때만 다음 날로 넘깁니다.
   - '점심 식사', '저녁 식사'로 확정된 시간에는 배치하지 않습니다.
   - 과제를 가능한 한 빨리 끝내도록, 가장 이른 빈 시간부터 차례로 채웁니다. (급한 과제가 새로 생기면 나중에 재조정합니다)
     도구가 '[하루 배치 한도]'를 알려준 경우에만 그 한도를 넘지 않게 여러 날로 나눕니다.
4. submit_plan 도구에 JSON 배열 문자열로 제출하세요:
   [{"task_id":"T1","date":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM"}]
5. Validator가 REVISE와 위반 사항을 보냈다면, 지적된 부분을 고친 '전체' 계획을 다시 제출하세요.
   작업 목록이 다시 등록되었다면(REANALYZE 이후) get_free_slots를 다시 호출해 새 작업 기준으로 계획하세요.
6. 도구 호출이 끝난 뒤의 응답은 "계획을 제출했습니다."처럼 한 줄로만 쓰세요. 계획 전체를 다시 쓰지 말고,
   승인 여부를 스스로 말하지 마세요. (승인은 Validator가 도구로 검증한 뒤에 정해집니다)
7. 스스로 승인하지 마세요. 기존 계획을 미뤄도 계획이 불가능하면 submit_plan 대신 그 이유를 짧게 설명하세요."""

VALIDATOR_PROMPT = """당신은 Analyzer의 작업 단계와 Planner의 일정을 함께 검토하는 Validator입니다.
모든 응답은 한국어로 작성하세요.
1. 차례가 올 때마다 반드시 validate_plan 도구를 먼저 호출하세요. 도구 결과 없이 스스로 판단하지 마세요.
   Planner가 "승인되었다"고 말하더라도 믿지 말고, 다시 제출된 계획은 매번 도구로 검증합니다.
2. 도구 결과에 '[작업 단계 대조용]' 내용이 있으면, 과제가 요구하는 제출물이 작업 목록에 모두 있는지 비교하세요.
   분명히 빠진 제출물이 있거나 단계 구성이 잘못된 경우에만 request_reanalysis 도구를 호출하고 사유를 적으세요.
   사소한 표현 차이나 취향 문제로는 호출하지 마세요. 일정 충돌, 식사 시간, 작업이 끊긴 문제는 재분해 대상이 아니라
   Planner 반려(REVISE)로 해결합니다. 작업을 반으로 쪼개 달라는 요청도 하지 마세요.
3. 기본 식사 시간은 이미 비워져 있습니다. 수업 때문에 평소 식사 시간대에 먹을 수 없는 날에 계획이 식사할 자리를
   모두 차지했다면, 도구 결과의 '[식사 시간]'에 그 날과 후보가 나옵니다. 그런 날이 있으면, 날마다 어느 시간대를 비울지 직접 판단해
   reserve_meal 도구로 확정하세요. (date=YYYY-MM-DD, meal='점심' 또는 '저녁', start=HH:MM)
   - 제시된 후보 중에서 고르되, 식사가 너무 늦거나 이르지 않은지, 앞뒤 수업과 이동, 작업 흐름을 덜 끊는지를 따져 정합니다.
   - 후보가 '이른 식사'와 '늦은 식사'뿐이면 그날 일정을 보고 더 나은 쪽을 고릅니다.
   - 식사를 정해야 하는 날이 여러 개면 reserve_meal을 날마다 한 번씩, 한꺼번에 모두 호출하세요.
   - 확정한 뒤에는 REVISE로 반려하면서, 어느 날 몇 시를 식사 시간으로 비워야 하는지 Planner에게 알려 주세요.
4. 최종 상태에 따라 응답하세요.
   - 작업 단계 재분해가 필요하면(도구 판정 또는 request_reanalysis 접수): 첫 줄에 REANALYZE,
     이어서 Analyzer가 어떤 작업을 어떻게 다시 나눠야 하는지 구체적으로 적습니다.
   - 'PASS'이면: 첫 줄에 APPROVE, 이어서 승인 이유를 한 줄로 적습니다.
   - 'FAIL'이고 다음 단계가 반려이면: 첫 줄에 REVISE, 이어서 위반 사항마다 무엇을 어떻게 고쳐야 하는지 구체적으로 적습니다.
   - 'EMERGENCY'이면(시간이 절대적으로 부족해 시스템이 비상 계획을 세운 경우): 첫 줄에 APPROVE,
     이어서 마감까지 남은 빈 시간을 전부 배치했다는 것과 몇 시간이 부족한지를 적습니다.
   - 'INFEASIBLE' 또는 '재시도 한도 초과'이면: 첫 줄에 STOP, 이어서 실패 사유를 적습니다.
도구 결과와 다른 판정을 내리지 마세요."""

EXPORTER_PROMPT = """당신은 최종 결과를 정리하는 Exporter입니다.
모든 응답은 한국어로 작성하세요.
1. 이전 Agent의 응답(APPROVE, STOP 등)과 관계없이, 가장 먼저 반드시 export_plan 도구를 호출하세요. 다른 Agent의 말을 따라 하지 마세요.
2. 계획이 승인되어 파일이 생성되었다면: 생성된 파일 경로와 날짜별 일정을 보기 좋게 요약하고,
   미룬 기존 계획이 있으면 무엇이 언제로 옮겨졌는지도 알려준 뒤, 마지막 줄에 PLAN_COMPLETE 를 적으세요.
   도구 결과에 '비상 계획'이 있으면, 시간이 부족해 마감까지 모든 빈 시간을 배치했다는 것과 부족한 시간을 가장 먼저 알리세요.
3. 실패 보고서가 생성되었다면: 실패 사유와 제안을 요약하고, 마지막 줄에 PLAN_FAILED 를 적으세요."""

# Planner의 긴 도구 결과(빈 시간표)는 다른 Agent에게 넘기지 않고, 계획 제출 결과만 전달한다.
_PLANNER_SUMMARY_TOOLS = {"submit_plan"}


def _validator_guard(ctx: PlanContext) -> Callable[[], str | None]:
    """Validator가 validate_plan을 호출하지 않았거나 식사 시간을 정하지 않은 경우, 시스템이 대신 처리한 결과를 돌려준다."""
    verdicts = {"approved": "APPROVE", "failed": "STOP", "reanalyze": "REANALYZE", "planning": "REVISE"}

    def guard() -> str | None:
        notes = []
        if not ctx.validated_this_round:
            result = tool_functions.validate_plan(ctx).split("\n[작업 단계 대조용]")[0].rstrip()
            ctx.log("Validator가 validate_plan을 호출하지 않아 시스템이 직접 검증하고, 그 결과를 전달합니다.")
            notes.append("(Validator가 검증 도구를 호출하지 않아 시스템이 직접 검증한 결과입니다)\n" + result)
        for issue in list(ctx.pending_meals):  # 비울 식사 시간대를 정하지 않았으면 첫 후보로 확정
            start = issue.candidates[0][0]
            answer = tool_functions.reserve_meal(ctx, issue.day.isoformat(), issue.meal.name, f"{start // 60:02d}:{start % 60:02d}")
            ctx.log(f"Validator가 {issue.day:%m/%d} {issue.meal.name} 시간을 정하지 않아 시스템이 첫 후보로 확정합니다.")
            notes.append(answer.split(" Planner에게")[0])
        if not notes:
            return None
        return verdicts.get(ctx.status, "REVISE") + "\n" + "\n".join(notes)

    return guard


def _exporter_guard(ctx: PlanContext) -> Callable[[], str | None]:
    """Exporter가 export_plan을 호출하지 않았으면 시스템이 대신 내보내고 그 결과를 돌려준다."""

    def guard() -> str | None:
        if ctx.output_files:
            return None
        result = tool_functions.export_plan(ctx)
        ctx.log("Exporter가 export_plan을 호출하지 않아 시스템이 직접 내보냈습니다.")
        return result + "\n" + ("PLAN_COMPLETE" if ctx.status == "approved" else "PLAN_FAILED")

    return guard


def build_agents(ctx: PlanContext, model_client: ChatCompletionClient) -> dict[str, AssistantAgent]:
    tools = build_tools(ctx)

    analyzer = AssistantAgent(
        "analyzer",
        description="과제 명세를 읽고 세부 작업, 예상 시간, 마감을 추출하는 에이전트",
        model_client=model_client,
        tools=[tools["read_assignment"], tools["submit_tasks"]],
        system_message=ANALYZER_PROMPT,
        max_tool_iterations=5,
        # 도구 호출 한도를 다 쓴 뒤의 요약 호출(tool_choice=none)에서 모델이 도구를 또 부르면 API 오류가 나므로
        # 요약 호출 없이 마지막 도구 결과를 그대로 전달한다.
        reflect_on_tool_use=False,
        tool_call_summary_formatter=lambda call, result: result.content,
    )
    planner = AssistantAgent(
        "planner",
        description="빈 시간표에 작업 세션을 배치하고, 시간이 부족하면 기존 계획을 미루는 에이전트",
        model_client=model_client,
        tools=[tools["get_free_slots"], tools["postpone_existing_plans"], tools["submit_plan"]],
        system_message=PLANNER_PROMPT,
        # 평소에는 빈 시간 조회 → 계획 제출의 2단계로 끝난다. 한도를 늘리면 제출 뒤에 모델 호출이 한 번 더 생기므로
        # 2회로 두고, 기존 계획을 미룬 경우에만 라우터가 Planner를 한 번 더 실행한다(workflow.py).
        # 제출 후 요약 생성(reflection)도 하지 않아, 긴 대화를 다시 보내 Groq 분당 토큰 한도를 넘는 일을 막는다.
        max_tool_iterations=2,
        reflect_on_tool_use=False,
        tool_call_summary_formatter=lambda call, result: (
            result.content if call.name in _PLANNER_SUMMARY_TOOLS else f"[{call.name} 호출 완료]"
        ),
    )
    validator = GuardedAssistantAgent(
        "validator",
        guard=_validator_guard(ctx),
        description="작업 단계와 계획을 규칙 기반 도구로 검증하고 승인/반려/재분해를 판정하며, 비울 식사 시간대를 정하는 에이전트",
        model_client=model_client,
        tools=[tools["validate_plan"], tools["request_reanalysis"], tools["reserve_meal"]],
        system_message=VALIDATOR_PROMPT,
        max_tool_iterations=4,  # validate_plan → (필요 시) request_reanalysis / reserve_meal. 텍스트로 답하면 바로 끝난다
        reflect_on_tool_use=True,
    )
    exporter = GuardedAssistantAgent(
        "exporter",
        guard=_exporter_guard(ctx),
        description="최종 계획을 캘린더 파일로 내보내거나 실패 보고서를 작성하는 에이전트",
        model_client=model_client,
        tools=[tools["export_plan"]],
        system_message=EXPORTER_PROMPT,
        max_tool_iterations=1,
        reflect_on_tool_use=True,
    )
    return {"analyzer": analyzer, "planner": planner, "validator": validator, "exporter": exporter}
