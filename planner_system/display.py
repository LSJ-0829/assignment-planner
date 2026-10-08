"""실행 과정을 간결한 '표시용 이벤트'로 바꾸고, 터미널에 출력한다.

AutoGen 기본 Console은 도구 결과 전문(과제 원문, 빈 시간표 전체 등)과 모델의 추론(Thought)까지 모두 출력한다.
여기서는 Agent 이름, 호출한 도구와 결과 요약, Agent의 짧은 응답만 남긴다.
같은 이벤트를 터미널(print_event)과 웹 화면(webapp.py)이 함께 사용한다.

표시용 이벤트(dict)
  {"type": "request", "text"}                       사용자 요청
  {"type": "agent", "agent", "label"}               Agent의 차례 시작
  {"type": "tool_call", "agent", "name"}            도구 호출
  {"type": "tool_result", "agent", "name", "lines"} 도구 결과 요약
  {"type": "message", "agent", "lines", "more"}     Agent의 응답 (more: 생략한 줄 수)
  {"type": "log", "text"}                           시스템 로그 (라우터 판정, 보정, 재시도 등)
  {"type": "result", "result"}                      실행 종료 (AutoGen TaskResult)
"""
from __future__ import annotations

from typing import AsyncIterator

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import (
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)

AGENT_LABELS = {
    "analyzer": "Analyzer · 과제 분석",
    "planner": "Planner · 일정 배치",
    "validator": "Validator · 검증",
    "exporter": "Exporter · 결과 정리",
}
BOLD, CYAN, YELLOW, GRAY, RESET = "\033[1m", "\033[36m", "\033[33m", "\033[90m", "\033[0m"
MAX_LINES = 6  # Agent 응답은 앞부분만 보여준다 (exporter 제외)


def _clip(line: str, width: int = 160) -> str:
    return line if len(line) <= width else line[: width - 1] + "…"


def summarize_tool_result(name: str, content: str) -> list[str]:
    """도구 결과에서 사람이 볼 핵심만 추린다."""
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return []
    if lines[0].startswith("오류"):
        return [lines[0]]
    if name == "read_assignment":
        body = content.split("-" * 40, 1)[-1].strip()
        extra = [ln for ln in lines if ln.startswith(("[사용자 입력 마감]", "[사용자 설명]", "[주의]"))]
        return [f"과제 파일을 읽었습니다 ({len(body)}자)"] + extra
    if name == "get_free_slots":
        days = sum(1 for ln in lines if ln[:4].isdigit())
        marker = next((i for i, ln in enumerate(lines) if ln.startswith("[마감별")), len(lines))
        return [f"{days}일치 빈 시간 계산 완료 (마감별 필요/가능 시간)"] + lines[marker + 1:]
    if name == "postpone_existing_plans":
        marker = next((i for i, ln in enumerate(lines) if ln.startswith("[등록된 작업]")), len(lines))
        return lines[:marker][:12]  # 미룬 내역만 (뒤따르는 빈 시간표는 생략)
    if name == "validate_plan":
        marker = next((i for i, ln in enumerate(lines) if ln.startswith(("경고", "다음 단계"))), len(lines))
        return lines[:marker][:14]  # 판정과 위반 사항만 (경고와 과제 원문 발췌는 생략)
    if name == "export_plan":
        return [ln for ln in lines if ln.startswith(("내보내기", "계획 생성 실패", "- 캘린더", "- 계획표", "- 계획 저장소", "- 비상 계획", "- 미룬", "  ·"))]
    return lines[:10]  # submit_tasks / submit_plan / request_reanalysis: 짧은 요약이므로 그대로


async def iter_display_events(stream, ctx=None) -> AsyncIterator[dict]:
    """AutoGen 실행 스트림을 표시용 이벤트로 바꾼다.

    ctx(PlanContext)를 넘기면 시스템 로그(라우터 판정 등)를 모아 두었다가, 해당 Agent의 출력 뒤에 순서대로 내보낸다.
    """
    if ctx is not None:
        ctx.defer_logs = True

    def drain_logs() -> list[dict]:
        if ctx is None:
            return []
        logs = [{"type": "log", "text": msg} for msg in ctx.pending_logs]
        ctx.pending_logs.clear()
        return logs

    current = None
    turn_finished = False  # 직전 Agent가 최종 메시지를 냈는가 (같은 Agent가 연달아 실행되는 경우 구분용)
    call_names: dict[str, str] = {}
    async for event in stream:
        if isinstance(event, TaskResult):
            for log in drain_logs():
                yield log
            yield {"type": "result", "result": event}
            return
        source = getattr(event, "source", "")
        if source == "user":
            if isinstance(event, TextMessage):
                yield {"type": "request", "text": event.content}
            continue
        if source in AGENT_LABELS and (source != current or turn_finished):
            for log in drain_logs():
                yield log
            current, turn_finished = source, False
            yield {"type": "agent", "agent": source, "label": AGENT_LABELS[source]}
        if isinstance(event, (TextMessage, ToolCallSummaryMessage)):
            turn_finished = True  # 한 차례의 마지막 메시지. 다음 이벤트는 새 차례다.

        if isinstance(event, ToolCallRequestEvent):
            for call in event.content:
                call_names[call.id] = call.name
                yield {"type": "tool_call", "agent": source, "name": call.name}
        elif isinstance(event, ToolCallExecutionEvent):
            for res in event.content:
                name = call_names.get(res.call_id, res.name)
                yield {"type": "tool_result", "agent": source, "name": name,
                       "lines": [_clip(ln) for ln in summarize_tool_result(name, res.content)]}
        elif isinstance(event, TextMessage):
            lines = event.content.strip().splitlines()
            if not lines:
                continue
            limit = None if source == "exporter" else MAX_LINES
            shown = lines[:limit]
            yield {"type": "message", "agent": source, "lines": [_clip(ln, 200) for ln in shown],
                   "more": len(lines) - len(shown)}
        # ThoughtEvent(모델 추론), ToolCallSummaryMessage(도구 결과 중복) 등은 표시하지 않는다.
    for log in drain_logs():
        yield log


def print_event(event: dict) -> None:
    """표시용 이벤트 하나를 터미널에 출력한다."""
    kind = event["type"]
    if kind == "request":
        print(f"{BOLD}▶ 요청{RESET} {event['text']}")
    elif kind == "agent":
        print(f"\n{BOLD}{CYAN}[{event['label']}]{RESET}")
    elif kind == "tool_call":
        print(f"  {YELLOW}🔧 {event['name']}{RESET}")
    elif kind == "tool_result":
        for i, ln in enumerate(event["lines"]):
            print(f"     {'└ ' if i == 0 else '  '}{ln}")
    elif kind == "message":
        for ln in event["lines"]:
            print(f"  {ln}")
        if event["more"]:
            print(f"  {GRAY}… ({event['more']}줄 생략){RESET}")
    elif kind == "log":
        print(f"{GRAY}[system] {event['text']}{RESET}", flush=True)


# ----- 실행 결과(TaskResult) 분석: 4주차 실습 5-1, 5-2 ---------------------------------
def analyze_result(result: TaskResult) -> list[dict]:
    """대화 흐름과 메시지별 토큰 사용량을 표로 만든다. (수업의 '실행 결과 분석', '토큰 사용량 추적' 코드 기반)"""
    rows = []
    for i, msg in enumerate(result.messages):
        content = getattr(msg, "content", "")
        preview = content[:80] if isinstance(content, str) else str(content)[:80]
        usage = getattr(msg, "models_usage", None)
        rows.append({
            "index": i, "source": msg.source, "type": type(msg).__name__, "preview": preview.replace("\n", " "),
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
        })
    return rows


def print_trace(rows: list[dict]) -> None:
    print("=" * 60 + "\n대화 흐름 분석\n" + "=" * 60)
    for r in rows:
        print(f"  [{r['index']:2d}] {r['source']:10s} | {r['type']:24s} | {r['preview']}...")
    print("\n" + "=" * 60 + "\n토큰 사용량 분석\n" + "=" * 60)
    total_prompt = total_completion = 0
    for r in rows:
        if r["prompt_tokens"] or r["completion_tokens"]:
            total_prompt += r["prompt_tokens"]
            total_completion += r["completion_tokens"]
            print(f"  [{r['source']:10s}] 입력: {r['prompt_tokens']:,}  출력: {r['completion_tokens']:,}")
    print(f"\n총 입력 토큰: {total_prompt:,}\n총 출력 토큰: {total_completion:,}\n총 토큰: {total_prompt + total_completion:,}")
