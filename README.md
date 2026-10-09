# Assignment Planner — 과제 수행 계획 Multi Agent System

과제 명세서(PDF/텍스트)를 넣으면, 과제를 세부 작업으로 나누고 **수업·약속·식사·다른 과제와 겹치지 않으면서 마감 안에 끝나는** 수행 계획을 세워 주는 AutoGen 기반 Multi Agent System입니다. 결과는 캘린더 파일(`.ics`)과 주간 시간표로 받습니다.

- Agent 4개(Analyzer, Planner, Validator, Exporter)가 **GraphFlow**로 협업합니다.
- 계산과 검증은 LLM이 아니라 **Tool 9개**(Python 코드)가 맡고, 다음 단계는 Tool의 판정으로 결정합니다.
- 검증에서 문제가 나오면 Planner(일정 재배치) 또는 Analyzer(작업 재분해)로 되돌아갑니다.

## 빠른 시작

Python 3.10 이상만 있으면 됩니다. **명령 하나로 설치와 실행이 함께 됩니다.**

```bash
git clone https://github.com/LSJ-0829/assignment-planner.git
cd assignment-planner
bash run.sh            # Windows: run.bat (더블클릭도 가능)
```

제출 ZIP으로 받았다면 압축을 푼 뒤 `code` 폴더에서 `bash run.sh`(Windows: `run.bat`)를 실행합니다.

1. 처음 실행하면 가상환경(`.venv`)을 만들고 필요한 패키지를 자동으로 설치합니다. (1~3분)
2. Groq API 키가 없으면 터미널에서 한 번 물어보고 `.env`에 저장합니다. 키는 [Groq Console](https://console.groq.com/keys)에서 무료로 발급받습니다.
3. 주소창이 없는 **앱 창**이 열립니다. (설치된 Edge/Chrome의 앱 모드를 사용합니다. 창을 닫으면 프로그램이 끝납니다.)

앱 창에서 하는 일:

| 순서 | 할 일 |
|---|---|
| 1 | **매주 반복 일정** 카드에 수업 시간표를 추가합니다. (요일, 시각, 시작일, 반복 횟수) 시간표 없이 시작해도 됩니다. |
| 2 | **과제 PDF를 창에 끌어다 놓습니다.** 문서에 마감일이 없으면 마감일을, 내용을 읽을 수 없으면(이미지 PDF) 과제 설명을 입력합니다. |
| 3 | **계획 세우기**를 누르면 Agent들의 진행 과정이 실시간으로 표시되고, 승인된 계획이 주간 시간표에 나타납니다. |

터미널만 쓰려면 `bash run.sh --assignment 과제.pdf`처럼 옵션을 붙입니다. (아래 [터미널 사용법](#터미널-사용법))

## 시스템 구조 (GraphFlow)

```
    ┌──────────(작업 단계 문제 → 재분해, 최대 1회)──────────┐
    ▼                                                      │
analyzer ──(작업 등록 성공)──► planner ──► validator ──(PASS / 실패 확정)──► exporter
    │                           ▲ ↺ ▲           │
    └──(작업 등록 실패)──► exporter  │ └──(일정 문제 → 재배치, 최대 3회)─┘
                                  └ 기존 계획을 미룬 직후 한 번 더 실행
```

| Agent | 역할 | Tool (호출 시점) |
|---|---|---|
| analyzer | 과제 명세를 읽고 세부 작업·예상 시간·마감·선행 관계를 추출. 재분해 요청을 받으면 작업을 다시 나눔 | `read_assignment` (분석 시작 시), `submit_tasks` (작업을 나눈 뒤, 재분해 시 다시) |
| planner | 빈 시간에 작업 세션을 배치. 시간이 부족하면 덜 중요한 다른 과제의 계획을 뒤로 미룸 | `get_free_slots` (배치 전), `postpone_existing_plans` (시간 부족 시), `submit_plan` (배치 후, 반려 시 다시) |
| validator | 작업 단계와 일정을 규칙 기반 도구로 검증하고 승인 / 재배치 반려 / 재분해 반려 / 중단을 판정. 식사 시간이 밀리는 날은 비울 시간대를 판단 | `validate_plan` (계획이 제출될 때마다), `request_reanalysis` (과제가 요구한 제출물이 작업 목록에 빠졌을 때), `reserve_meal` (식사할 자리가 없는 날, 비울 시간대를 정할 때) |
| exporter | 승인된 계획을 `.ics`/`.md`로 저장하고 계획 저장소에 반영. 실패 시 실패 보고서 작성 | `export_plan` (마지막에 한 번) |

**설계 원칙**

- **라우팅은 LLM이 쓴 문자열이 아니라 도구가 기록한 상태로 결정합니다.** Validator가 "APPROVE"라고 써도 검증 도구의 판정이 FAIL이면 반려됩니다.
- **Agent가 필수 도구를 건너뛰어도 결과가 왜곡되지 않습니다.** Validator가 검증 도구 없이 답하면 시스템이 그 자리에서 검증하고, Validator의 발화를 실제 검증 결과로 바꿔 전달합니다. (Exporter도 같습니다.)
- **결과 평가·수정 루프가 두 단계입니다.** 일정 문제는 planner로, 작업 분해 문제는 analyzer로 되돌아갑니다.

**종료 조건**

- 정상 종료: exporter가 결과 파일을 저장하고 `PLAN_COMPLETE` 또는 `PLAN_FAILED`를 선언합니다. exporter는 그래프의 마지막 노드라 그래프 실행도 함께 끝납니다.
- 안전 종료: 메시지 30개(`MaxMessageTermination`), 600초(`TimeoutTermination`), 일정 검증 최대 3회, 작업 재분해 최대 1회.

**Failure Case 처리**

| 상황 | 대응 |
|---|---|
| 마감까지 시간이 절대적으로 부족 | **비상 계획**: 미룰 수 있는 기존 계획을 모두 미루고, 마감까지 남은 빈 시간을 전부 그 과제에 배치 (세부 단계 없이 과제 이름만). 부족한 시간을 함께 알림 |
| 마감까지 빈 시간이 전혀 없음, 재시도 한도 초과 | 실패 보고서 작성 (마감별 필요/가용 시간, 위반 사항 포함) |
| 시간은 부족하지만 다른 과제 계획을 미루면 해결됨 | `postpone_existing_plans`로 미룬 뒤 다시 계획 |
| 과제 파일 없음 / 이미지 PDF / 문서에 마감 없음 | 실패 보고서 / 과제 설명 입력 요구 / 마감 입력 요구 |
| Agent가 도구 호출을 빠뜨리거나 거짓 승인 | 시스템이 직접 검증·내보내기하고 실제 결과를 전달 |
| Planner가 계획을 도구 대신 텍스트로 보냄, JSON 형식 오류 | 시스템이 대신 제출 / 오류 메시지를 돌려줘 다시 제출 |
| Groq 분당 토큰 한도, 하루 한도, 도구 호출 형식 오류(400) | 60초 대기 후 재시도 / 안내 후 종료 / 재시도 후 건너뜀 |

## 수업 자료와의 대응

수업 실습 노트북(2~4주차)의 코드를 바탕으로 만들었습니다.

| 수업 자료 | 이 프로젝트에서 쓰인 곳 |
|---|---|
| 3·4주차 환경 설정: `load_dotenv`, `GROQ_API_KEY`, `OpenAIChatCompletionClient(base_url=Groq, model_info=…)` | `planner_system/config.py`의 `create_model_client()` |
| 3주차 3-1: `AssistantAgent(name, model_client, system_message, description)` | `planner_system/agents.py`의 `build_agents()` |
| 4주차 1-1: 타입 힌트·독스트링이 있는 `async def` 함수를 도구로 등록, `reflect_on_tool_use` | `planner_system/tools.py`의 `build_tools()`, `agents.py` |
| 4주차 2-1: `FunctionTool(func=, name=, description=)` | `tools.py`의 `build_tools()` |
| 4주차 2-2: 도구는 예외를 던지지 말고 오류를 문자열로 반환 | `tools.py`의 모든 도구 (`"오류: …"` 반환 후 LLM이 다시 제출) |
| 4주차 2-3: 한 Agent가 여러 도구를 순차 호출 | analyzer(`read_assignment` → `submit_tasks`), planner(`get_free_slots` → `submit_plan`), `max_tool_iterations` |
| 3주차 7-1: GraphFlow 조건 분기 + 루프 (`DiGraphBuilder`, `add_edge(condition=lambda msg: …)`) | `planner_system/workflow.py`의 `build_flow()` — Writer↔Reviewer 루프를 Planner↔Validator, Analyzer↔Validator로 확장 |
| 3주차 7-3: 분류 결과에 따른 조건 분기 | `workflow.py`의 `Router` — validator 다음에 planner / analyzer / exporter로 분기 |
| 4주차 3-2: `TextMentionTermination \| MaxMessageTermination \| TimeoutTermination` (안전 장치 패턴) | `workflow.py`의 `build_termination()` |
| 3주차 4-1: Writer + Critic (Reflection Pattern), 2주차 Reflexion | planner가 만든 계획을 validator가 검토하고 반려하는 구조 |
| 4주차 5-1, 5-2: `TaskResult` 대화 흐름·토큰 사용량 분석 | `planner_system/display.py`의 `analyze_result()`, `--trace` 옵션 |
| 4주차 5-3: AutoGen 로깅 | `--debug` 옵션 |
| 3·4주차: `await Console(team.run_stream(task=…))` | `--console` 옵션 (기본은 요약 출력) |
| 2주차 메모리, 4주차 4장 상태 저장 | 승인된 계획을 `data/plans.json`에 누적해 다음 계획에 반영 |

수업에서 쓴 `meta-llama/llama-4-scout-17b-16e-instruct` 모델은 현재 Groq에서 제공되지 않아, 도구 호출이 안정적인 `openai/gpt-oss-120b`를 기본값으로 씁니다.

## 터미널 사용법

`run.sh`(Linux/macOS/WSL)와 `run.bat`(Windows)은 옵션을 그대로 `main.py`에 넘깁니다. 가상환경을 직접 켰다면 `python main.py …`로 써도 됩니다.

```bash
# 계획 세우기
bash run.sh --assignment 과제.pdf                          # 문서에 마감이 있으면 그대로 사용
bash run.sh --assignment 과제.pdf --deadline "10/18"        # 마감 직접 입력 (10/18, 10월 18일, 2026-10-18 23:59)
bash run.sh --assignment 이미지과제.pdf --deadline 10/1 --note "전기자기학1 과제, 서술형 2문제"
bash run.sh --assignment 급한과제.pdf --deadline 10/9 --priority high   # 시간이 부족하면 다른 과제 계획을 미룸
bash run.sh --assignment data/urgent_assignment.txt --deadline "<내일 날짜> 23:59"   # 시간 절대 부족 → 비상 계획 (예시 과제)

# 매주 반복 일정 (시간표)
bash run.sh --add-weekly "화 18:00-21:45 인공지능프로젝트" --repeat 16 --from 2026-09-01 --short 인지개
bash run.sh --add-weekly "토 12:00-20:00 아르바이트" --off-campus    # 횟수 생략 = 계속 반복, 학교 밖 일정
bash run.sh --list-weekly
bash run.sh --remove-weekly <ID>

# 특정 날짜의 약속
bash run.sh --add-event "10/9 18:00-20:00 저녁 약속"
bash run.sh --list-events
bash run.sh --remove-event <ID>

# 보기·관리 (API 불필요)
bash run.sh --week                 # 이번 주 시간표 (터미널 + outputs/week_*.html)
bash run.sh --list-plans           # 저장된 계획
bash run.sh --remove-plan <ID 또는 파일명>
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--assignment` | 과제 명세 파일 (.pdf/.txt/.md) | `data/assignment1.pdf`가 있으면 그것 |
| `--deadline` | 최종 마감. 입력하면 문서의 날짜보다 우선. 문서에 날짜가 없는데 생략하면 실행 중에 물어봄 | 없음 |
| `--note` | 과제 설명 (이미지 PDF처럼 내용을 읽을 수 없을 때) | 없음 |
| `--priority` | 이 과제의 우선순위 (`low` / `normal` / `high`). 우선순위가 낮거나 같은 다른 과제의 계획만 미룰 수 있음 | `normal` |
| `--start` | 계획 시작 시점 `YYYY-MM-DD HH:MM` | 현재 시각 |
| `--max-attempts` / `--max-reanalysis` | 일정 검증 최대 횟수 / 작업 재분해 최대 횟수 | 3 / 1 |
| `--no-save` | 승인된 계획을 저장소에 저장하지 않음 (시험 실행) | - |
| `--add-weekly` `--repeat` `--from` `--short` `--off-campus` | 매주 반복 일정 추가: 반복 횟수, 시작일, 약어, 학교 밖 여부 | 계속 반복, 오늘부터 |
| `--console` / `--trace` / `--debug` | AutoGen Console 전체 출력 / 대화 흐름·토큰 분석 / 내부 로그 | - |
| `--app` / `--web [PORT]` | 화면을 앱 창 / 브라우저 주소(`http://localhost:8000`)로 실행 | 옵션이 없으면 `--app` |
| `--timetable` `--events` `--plans` `--output` | 데이터 파일과 결과 폴더 위치 | `data/…`, `outputs/` |

결과는 `outputs/`에 저장됩니다. 성공하면 `study_plan_*.ics`(구글/애플 캘린더에서 가져오기)와 `study_plan_*.md`, 실패하면 `failure_report_*.md`입니다.

## 계획이 만들어지는 규칙

- **가능한 한 빨리 끝내기**: 가장 이른 빈 시간부터 채웁니다. 나중에 급한 과제가 생기면 덜 급한 기존 계획을 뒤로 미뤄 재조정합니다.
- **빈 시간**: 활동 시간대(기본 09:00~24:00)에서 수업·약속, 등교·귀가 이동 시간, 식사 시간, 다른 과제의 저장된 계획을 뺀 시간입니다.
- **식사 시간**: 점심(11:30~14:00 사이 60분, 기본 12:00)과 저녁(17:30~20:30 사이 60분, 기본 18:00)을 미리 비워 둡니다. 수업이 그 시간대를 막는 날은 수업 직전(이른 식사)이나 직후(늦은 식사) 중 한 곳이 비어 있어야 하고, 계획이 두 자리를 모두 차지하면 **validator가 어느 쪽을 비울지 판단해 `reserve_meal`로 확정**합니다.
- **작업 연속성**: 한 작업은 가능하면 같은 날에 이어서 배치합니다. 식사·수업을 사이에 두고 이어 가는 것은 괜찮습니다. 하루 안에 끝낼 수 있는데도 떨어뜨려 놓으면 첫 검증에서 반려합니다.
- **시간이 절대적으로 부족한 과제**: 계획을 포기하지 않고 비상 계획을 세웁니다. 마감까지의 모든 빈 시간(수업·약속·식사 제외)을 그 과제로 채우고, 필요 시간 대비 몇 시간이 부족한지 계획표와 화면에 표시합니다.
- **작업 단계 검증**: 8시간을 넘는 작업, 단계가 너무 적은 분해, 같은 이름의 작업, 순환하는 선행 관계를 잡아냅니다.
- **여러 과제**: 승인된 계획은 `data/plans.json`에 쌓이고, 다음 과제는 그 시간을 피해서 계획합니다. 같은 과제 파일을 다시 실행하면 새 계획으로 교체합니다.
- **기존 계획 미루기**: 미룰 수 있는 것은 우선순위가 낮거나 같은 과제의, 아직 시작하지 않은 세션뿐입니다. 옮긴 뒤에도 그 작업의 마감과 작업 순서를 지키고, 새 계획이 승인될 때만 저장합니다.
- **약속·반복 일정을 나중에 추가하면**: 겹치게 된 기존 과제 계획을 마감 안의 다음 빈 시간으로 자동으로 옮기고 알려 줍니다.

## 데이터 파일 (`data/`)

| 파일 | 내용 | 만드는 방법 |
|---|---|---|
| `timetable.json` | 매주 반복 일정과 선호 설정. **없으면 빈 시간표로 시작** | 화면의 "매주 반복 일정" 또는 `--add-weekly` (형식은 `timetable.example.json` 참고) |
| `events.json` | 특정 날짜의 약속 | 화면의 "약속 추가" 또는 `--add-event` |
| `plans.json` | 승인된 과제 계획 | 계획이 승인되면 자동 저장 |
| `uploads/` | 화면에서 끌어다 놓은 과제 파일 | 자동 |

`timetable.json`의 `preferences`에서 바꿀 수 있는 것: `day_start`/`day_end`(활동 시간대), `min_session_minutes`, `commute_before_minutes`/`commute_after_minutes`(학교 가는 날 첫 일정 전·마지막 일정 후 이동 시간), `meals`(식사 시간대, `[]`이면 끔), `recommended_daily_hours`(넣으면 여유가 있을 때 하루에 몰지 않고 나눔), `max_daily_hours`(하루 절대 상한).

개인 시간표·계획·과제 파일과 API 키(`.env`, `API_KEY.txt`)는 `.gitignore`에 있어 저장소에 올라가지 않습니다.

## API 키와 모델

- 필요한 환경 변수는 `GROQ_API_KEY` 하나입니다. 키는 아래 중 한 가지 방법으로 넣습니다.
  - 처음 실행할 때 터미널에 입력 → `.env`에 저장됩니다.
  - `.env.example`을 `.env`로 복사해 `GROQ_API_KEY=…`를 채우거나, 환경 변수로 설정합니다.
  - 제출 ZIP에 들어 있는 빈 `API_KEY.txt`(`code` 폴더의 바로 위)에 키만 한 줄로 붙여 넣습니다.
- `GROQ_MODEL` 환경 변수로 모델을 바꿀 수 있습니다. (기본 `openai/gpt-oss-120b`)
- Groq 무료 등급은 분당 8,000 토큰, 하루 200,000 토큰 한도가 있습니다. 한 번 실행에 약 2만~4만 토큰을 씁니다. 분당 한도에 걸리면 60초 기다렸다가 자동으로 재시도하고, 하루 한도에 걸리면 안내 후 종료합니다.

## 테스트 (API 키 불필요)

```bash
bash setup.sh                 # 설치 + 테스트를 한 번에 (Windows: setup.bat)
.venv/bin/python -m pytest tests
```

미리 정한 LLM 응답을 재생하는 `ReplayChatCompletionClient`로 GraphFlow 전체 흐름까지 검증합니다.

| 테스트 파일 | 확인하는 것 |
|---|---|
| `test_tools.py` | 빈 시간 계산, 충돌·마감·선행 순서 검증, 불가능한 일정 판정, 마감 직접 입력, `.ics` 생성 |
| `test_validation.py` | 작업 단계 검증, 재분해 판정과 한도 |
| `test_meals.py` | 식사 시간 확보와 비울 시간대 확정, 작업 연속성, 하루 권장 시간 |
| `test_rescheduling.py` | 기존 계획 미루기 (마감·순서·우선순위 준수, 승인 시에만 저장) |
| `test_timetable_store.py` | 시간표 없이 시작, 매주 반복 일정과 반복 횟수 |
| `test_plan_store.py`, `test_week_view.py`, `test_events_and_web.py` | 계획 누적, 주간 시간표, 약속, 화면 API |
| `test_workflow_offline.py` | GraphFlow 라우팅: 재배치 루프, 재분해 루프, 미룬 뒤 재계획, 거짓 승인 차단, 도구 누락 보정, 재시도 한도 |

## 폴더 구조

```
├── main.py                  # 실행 진입점 (터미널 명령, 화면 실행)
├── run.sh / run.bat         # 실행 (처음이면 자동 설치)
├── setup.sh / setup.bat     # 설치 + 테스트 확인 (선택)
├── requirements.txt
├── planner_system/
│   ├── agents.py            # 4개 Agent 정의와 프롬프트, 필수 도구 누락 보정(GuardedAssistantAgent)
│   ├── workflow.py          # GraphFlow 구성, 라우터(엣지 조건), Termination Condition
│   ├── tools.py             # Agent가 호출하는 Tool 9개와 FunctionTool 등록
│   ├── context.py           # Tool들이 공유하는 작업 상태(PlanContext)
│   ├── validation.py        # 검증 규칙 (작업 단계, 일정, 식사 시간, 작업 연속성)
│   ├── rescheduling.py      # 기존 계획 미루기, 새 일정과 겹친 계획 옮기기
│   ├── schedule.py          # Task/Session, 시간표 기반 빈 시간·실현 가능성·식사 시간 계산
│   ├── timetable_store.py   # 매주 반복 일정 저장소 (시작일, 반복 횟수)
│   ├── event_store.py       # 특정 날짜의 약속 저장소
│   ├── plan_store.py        # 승인된 계획 누적 저장소
│   ├── runner.py            # 계획 수립 실행기 (터미널·화면 공용)
│   ├── display.py           # 실행 과정 요약 출력, TaskResult 분석
│   ├── export.py            # .ics / 계획표 / 실패 보고서 생성
│   ├── week_view.py         # 주간 시간표 (터미널 + HTML)
│   ├── webapp.py            # 화면용 로컬 서버와 앱 창 실행
│   ├── static/index.html    # 화면 (PDF 끌어다 놓기, 진행 과정, 주간 시간표)
│   ├── config.py            # Groq 모델 클라이언트, API 키, 한도 초과 재시도
│   └── utils.py             # 시각·날짜 파싱, JSON 추출
├── data/                    # 시간표·약속·계획 저장소, 예시 과제
├── tests/                   # 단위/통합 테스트
└── outputs/                 # 실행 결과 (.ics, .md, 주간 시간표 HTML, 실행 로그)
```
