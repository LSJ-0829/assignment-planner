"""모델 클라이언트 설정 (Groq, OpenAI 호환 API).

3·4주차 실습의 '환경 설정' 코드(load_dotenv → GROQ_API_KEY → OpenAIChatCompletionClient)를 바탕으로,
실제 실행에서 만난 Groq API 오류(토큰 한도, 도구 호출 형식 오류)에 대한 안전장치를 더했다.
"""
import asyncio
import os
from pathlib import Path

import openai

from autogen_core.models import CreateResult, ModelFamily, RequestUsage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from dotenv import find_dotenv, load_dotenv

DEFAULT_MODEL = "openai/gpt-oss-120b"
CODE_DIR = Path(__file__).resolve().parent.parent
KEY_FILE_CANDIDATES = [CODE_DIR / "API_KEY.txt", CODE_DIR.parent / "API_KEY.txt"]


def load_api_key() -> str:
    """GROQ_API_KEY 환경 변수(.env 포함)를 우선 사용하고, 없으면 API_KEY.txt 파일에서 읽는다."""
    load_dotenv(find_dotenv(usecwd=True))
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    for path in KEY_FILE_CANDIDATES:
        if path.is_file():
            key = path.read_text(encoding="utf-8-sig").strip()
            if key:
                return key
    raise RuntimeError(
        "Groq API 키를 찾을 수 없습니다. 환경 변수 GROQ_API_KEY를 설정하거나 "
        "code/.env 또는 API_KEY.txt 파일에 키를 넣으세요."
    )


RATE_LIMIT_WAIT_SECONDS = 60
RATE_LIMIT_RETRIES = 3

# 재시도·대기 안내를 받을 함수. 기본은 터미널 출력이고, 실행기(runner.py)가 화면 표시용 이벤트로 바꿔 끼운다.
notice_handler = None


def _notice(message: str) -> None:
    if notice_handler is not None:
        notice_handler(message)
    else:
        print(f"\033[90m[system] {message}\033[0m", flush=True)


class GroqChatCompletionClient(OpenAIChatCompletionClient):
    """Groq API 오류에 대한 안전장치.

    - 분당 토큰 한도(TPM) 초과(413/429): 1분 기다린 뒤 다시 요청한다.
    - tool_use_failed(400): 도구 사용이 금지된 요약 호출에서 모델이 도구를 부르는 등 형식 오류. 한 번 재시도하고,
      그래도 실패하면 대체 텍스트를 반환해 워크플로가 멈추지 않게 한다. (라우팅은 도구 상태로 결정되므로 안전)
    """

    async def create(self, *args, **kwargs):
        tool_errors = 0
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return await super().create(*args, **kwargs)
            except openai.BadRequestError as e:
                if "tool_use_failed" not in str(e):
                    raise
                tool_errors += 1
                if tool_errors < 2:
                    _notice("모델의 도구 호출 형식 오류(tool_use_failed) → 재시도")
                    continue
                _notice("도구 호출 형식 오류가 반복되어 이 응답을 건너뜁니다.")
                return CreateResult(
                    finish_reason="stop",
                    content="(모델 응답 생성 실패: 도구 호출 형식 오류. 시스템이 도구 결과를 기준으로 다음 단계를 진행합니다.)",
                    usage=RequestUsage(prompt_tokens=0, completion_tokens=0),
                    cached=False,
                )
            except openai.APIStatusError as e:
                is_rate_limit = e.status_code in (413, 429) and "rate_limit" in str(e)
                per_day = "tokens per day" in str(e)  # 하루 한도(TPD)는 1분 대기로 회복되지 않는다
                if not is_rate_limit or per_day or attempt == RATE_LIMIT_RETRIES:
                    raise
                _notice(f"Groq 분당 토큰 한도 초과 → {RATE_LIMIT_WAIT_SECONDS}초 대기 후 재시도 "
                        f"({attempt + 1}/{RATE_LIMIT_RETRIES})")
                await asyncio.sleep(RATE_LIMIT_WAIT_SECONDS)


def has_api_key() -> bool:
    try:
        load_api_key()
        return True
    except RuntimeError:
        return False


def save_api_key(key: str) -> Path:
    """입력받은 키를 code/.env에 저장한다. (.env는 .gitignore에 있어 저장소·제출물에 포함되지 않는다)"""
    env_path = CODE_DIR / ".env"
    lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines() if not ln.startswith("GROQ_API_KEY=")] \
        if env_path.exists() else []
    env_path.write_text("\n".join([f"GROQ_API_KEY={key.strip()}"] + lines) + "\n", encoding="utf-8")
    os.environ["GROQ_API_KEY"] = key.strip()
    return env_path


def create_model_client() -> OpenAIChatCompletionClient:
    """수업 실습과 같은 방식으로 Groq 모델 클라이언트를 만든다.

    수업에서 쓴 meta-llama/llama-4-scout-17b-16e-instruct는 현재 Groq에서 제공되지 않아, 도구 호출이 안정적인
    openai/gpt-oss-120b를 기본값으로 쓴다. (GROQ_MODEL 환경 변수로 변경 가능)
    """
    api_key = load_api_key()  # .env도 여기서 로드되므로 GROQ_MODEL보다 먼저 호출
    model_client = GroqChatCompletionClient(
        model=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
        base_url="https://api.groq.com/openai/v1",
        api_key=api_key,
        temperature=0.2,
        max_tokens=8192,  # 추론 모델(gpt-oss)은 기본 한도(3072)에서 추론 도중 잘려 빈 응답을 반환함
        max_retries=5,  # Groq rate limit(429) 대비 재시도
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": ModelFamily.UNKNOWN,
        },
    )
    return model_client
