"""한 번의 계획 수립 실행. 터미널(main.py)과 웹 화면(webapp.py)이 함께 사용한다.

  missing_inputs(ctx)          문서에 없어서 사용자가 보충해야 하는 정보 확인
  run_planning(ctx, on_event)  GraphFlow 실행 → 표시용 이벤트 전달 → 오류 처리 → 결과 요약
"""
from __future__ import annotations

import re
from typing import Callable

from . import config, tools
from .config import create_model_client
from .context import PlanContext
from autogen_agentchat.ui import Console

from .display import analyze_result, iter_display_events
from .utils import contains_date
from .workflow import build_flow


def missing_inputs(ctx: PlanContext) -> list[str]:
    """보충이 필요한 입력: 'note'(문서를 읽을 수 없어 과제 설명 필요), 'deadline'(문서에 날짜 없음).

    과제 파일 자체를 열 수 없는 경우는 여기서 막지 않고 Analyzer 단계의 Failure Case로 처리한다.
    """
    try:
        text = ctx.extract_text()
    except (OSError, ValueError):
        return []
    missing = []
    if ctx.text_is_sparse() and not ctx.user_note:
        missing.append("note")
    if ctx.user_deadline is None and (ctx.text_is_sparse() or not contains_date(text)):
        missing.append("deadline")
    return missing


async def run_planning(ctx: PlanContext, on_event: Callable[[dict], None], raw_console: bool = False) -> dict:
    """워크플로를 끝까지 실행하고 요약을 반환한다. 어떤 경우에도 결과 파일(계획 또는 실패 보고서)을 남긴다.

    raw_console=True이면 수업 실습과 같은 방식(await Console(flow.run_stream(task=...)))으로 AutoGen의 전체 출력을 보여 준다.
    """
    task = (
        f"과제 파일 '{ctx.assignment_path.name}'을 분석하고, 내 시간표의 빈 시간에 맞춘 과제 수행 계획을 세워 주세요. "
        f"계획 시작 시점은 {ctx.start:%Y-%m-%d %H:%M} 입니다."
    )
    result = None
    model_client = None
    config.notice_handler = lambda message: on_event({"type": "log", "text": message})
    try:
        model_client = create_model_client()
        flow = build_flow(ctx, model_client)
        if raw_console:
            result = await Console(flow.run_stream(task=task))
        else:
            async for event in iter_display_events(flow.run_stream(task=task), ctx):
                if event["type"] == "result":
                    result = event["result"]
                else:
                    on_event(event)
    except Exception as e:  # API 키 없음, 인증 실패, 토큰 한도 초과 등
        message = re.sub(r"org_[a-z0-9]{8,}", "org_***", str(e))[:300]  # 계정을 식별하는 값은 화면·로그에 남기지 않는다
        on_event({"type": "log", "text": f"실행 중 오류: {type(e).__name__}: {message}"})
        if "tokens per day" in str(e):
            on_event({"type": "log", "text": "Groq 하루 토큰 한도를 모두 사용했습니다. 오류 메시지에 나온 시간만큼 기다린 뒤 "
                                              "다시 실행하세요. (GROQ_MODEL로 다른 모델을 지정할 수도 있지만 작은 모델은 "
                                              "도구 호출이 불안정할 수 있습니다.)"})
        if ctx.status != "approved":
            ctx.status = "failed"
            ctx.failure_reason = ctx.failure_reason or f"LLM API 오류: {type(e).__name__}"
    finally:
        config.notice_handler = None
        ctx.defer_logs = False
        for msg in ctx.pending_logs:
            on_event({"type": "log", "text": msg})
        ctx.pending_logs.clear()
        if model_client is not None:
            await model_client.close()

    # 안전 종료(메시지 상한, 시간 초과, API 오류)로 Exporter까지 가지 못해도 결과 파일은 남긴다.
    if not ctx.output_files:
        on_event({"type": "log", "text": "Exporter 단계 전에 종료되어 현재 상태로 결과를 저장합니다."})
        if ctx.status != "approved":
            ctx.status = "failed"
            ctx.failure_reason = ctx.failure_reason or f"안전 종료: {result.stop_reason if result else '오류'}"
        tools.export_plan(ctx)

    usage = [m.models_usage for m in result.messages if getattr(m, "models_usage", None)] if result else []
    return {
        "status": ctx.status,
        "failure_reason": ctx.failure_reason if ctx.status == "failed" else "",
        "stop_reason": result.stop_reason if result else None,
        "messages": len(result.messages) if result else 0,
        "tokens_in": sum(u.prompt_tokens for u in usage),
        "tokens_out": sum(u.completion_tokens for u in usage),
        "attempts": ctx.attempts,
        "max_attempts": ctx.max_attempts,
        "reanalysis": ctx.reanalysis_count,
        "max_reanalysis": ctx.max_reanalysis,
        "postponed": list(ctx.postponed_log) if ctx.status == "approved" else [],
        "emergency": ({"needed": ctx.emergency["needed"], "available": ctx.emergency["available"],
                       "shortage": ctx.emergency["shortage"]} if ctx.emergency and ctx.status == "approved" else None),
        "files": [str(f) for f in ctx.output_files],
        "trace": analyze_result(result) if result else [],
    }
