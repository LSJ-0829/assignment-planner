"""시각·날짜 변환과 LLM 출력(JSON) 파싱 유틸리티."""
from __future__ import annotations

import json
import re
from datetime import date, datetime

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
KOR_WEEKDAYS = "월화수목금토일"


def to_minutes(hhmm: str) -> int:
    """'HH:MM' → 자정 기준 분. '24:00'(자정)까지 허용한다."""
    h, m = (int(x) for x in hhmm.strip().split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"잘못된 시각: {hhmm}")
    return h * 60 + m


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_day(d: date) -> str:
    return f"{d.isoformat()}({KOR_WEEKDAYS[d.weekday()]})"


def parse_deadline(text: str) -> datetime:
    """Agent가 제출한 마감('YYYY-MM-DD HH:MM' 또는 'YYYY-MM-DD')을 해석한다. 시각 생략 시 23:59."""
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt if fmt == "%Y-%m-%d %H:%M" else dt.replace(hour=23, minute=59)
    raise ValueError(f"마감 형식 오류: '{text}' (YYYY-MM-DD HH:MM 형식 필요)")


def parse_user_deadline(text: str, start: datetime) -> datetime:
    """사용자가 입력한 마감을 해석한다. 연도를 생략하면 계획 시작 연도, 시각을 생략하면 23:59.

    허용 예: 2026-10-01 23:59, 2026-10-01, 10-01, 10/1, 10/1 18:00, 10월 1일, 10월 1일 18:00
    """
    m = re.fullmatch(
        r"(?:(\d{4})[-./]\s*)?(\d{1,2})\s*(?:[-./]|월)\s*(\d{1,2})\s*일?\s*(?:(\d{1,2}):(\d{2}))?", text.strip()
    )
    if not m:
        raise ValueError(f"마감 형식을 알 수 없습니다: '{text}' (예: 2026-10-01 23:59, 10/1, 10월 1일)")
    year = int(m.group(1)) if m.group(1) else start.year
    hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (23, 59)
    dt = datetime(year, int(m.group(2)), int(m.group(3)), hour, minute)
    if not m.group(1) and dt < start:  # 연도 생략 + 이미 지난 날짜 → 다음 해로 해석
        dt = dt.replace(year=year + 1)
    return dt


DATE_PATTERNS = [
    r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}",  # 2026. 10. 11 / 2026-10-11 / 2026년 10월 11
    r"\d{1,2}\s*월\s*\d{1,2}\s*일",  # 10월 11일
    r"\b\d{1,2}/\d{1,2}\b",  # 10/11
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",  # Oct 11
    r"(?i)\bdue\b",
]


def contains_date(text: str) -> bool:
    """과제 문서에 마감으로 볼 만한 날짜 표현이 있는지 확인한다."""
    return any(re.search(p, text) for p in DATE_PATTERNS)


def extract_json(text: str):
    """LLM이 ```json 블록, 설명 문장, 끝에 남는 괄호 등을 섞어 보내도 첫 JSON 값만 파싱한다."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise json.JSONDecodeError("JSON을 찾을 수 없음", text, 0)
    value, _ = json.JSONDecoder().raw_decode(text[min(starts):])  # 뒤따르는 잔여 문자는 무시
    return value
