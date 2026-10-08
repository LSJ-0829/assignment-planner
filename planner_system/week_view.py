"""주간 시간표 시각화 (API 불필요).

기준 날짜가 속한 주(월~일)의 수업, 고정 일정, 등교·귀가 이동, 저장된 과제 계획을
터미널 격자와 HTML 파일로 보여준다.
"""
from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .plan_store import PlanStore
from .schedule import Schedule
from .utils import KOR_WEEKDAYS, fmt_minutes, to_minutes

SLOT = 15  # 격자 한 줄 = 15분 (빈 시간은 한 줄에 점 하나)
CELL = 13  # 터미널 한 칸의 표시 폭 (7일 × 13 + 시간 열 6 ≈ 100칸)
COMMUTE_LABELS = {"등교 준비·이동": "등교", "귀가 이동": "하교"}  # 주간 시간표에서는 짧게 표시

# 종류별 (터미널 색 2단계, HTML 색 2단계, 범례 이름). 겹치면 앞쪽 종류를 우선 표시한다.
# 같은 종류는 같은 색 계열을 쓰고, 하루 안에서 시간순으로 진한/밝은 색을 번갈아 써서 연속된 블록을 구분한다.
KINDS = {
    "class": (("\033[48;5;25;97m", "\033[48;5;33;97m"), ("#3a5bd9", "#6f8ef5"), "수업"),
    "plan": (("\033[48;5;29;97m", "\033[48;5;36;97m"), ("#17804f", "#34b27a"), "과제 계획"),
    "event": (("\033[48;5;97;97m", "\033[48;5;134;97m"), ("#8f3ab3", "#b86ad8"), "고정 일정"),
    "commute": (("\033[48;5;240;97m", "\033[48;5;246;97m"), ("#6e737b", "#9ca1a8"), "이동"),
}
RESET, BOLD = "\033[0m", "\033[1m"


@dataclass
class Block:
    day: date
    start: int
    end: int
    name: str
    kind: str
    shade: int = 0  # 0: 진한 색, 1: 밝은 색
    title: str = ""  # 약어가 아닌 전체 이름 (툴팁·웹 화면용)

    @property
    def terminal_style(self) -> str:
        return KINDS[self.kind][0][self.shade]

    @property
    def html_color(self) -> str:
        return KINDS[self.kind][1][self.shade]


def week_of(d: date) -> list[date]:
    monday = d - timedelta(days=d.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def collect_blocks(timetable: dict, store: PlanStore | None, days: list[date]) -> list[Block]:
    schedule = Schedule(timetable, datetime.combine(days[0], datetime.min.time()))
    class_names = {c["name"] for c in timetable.get("classes", []) if c.get("at_school", True)}
    # 시간표에 약어(short)가 있으면 칸이 좁아도 읽히도록 약어로 표시한다.
    short = {x["name"]: x["short"] for x in timetable.get("classes", []) + timetable.get("events", []) if x.get("short")}
    short.update(COMMUTE_LABELS)
    blocks = []
    for d in days:
        for s, e, name in schedule.busy_intervals(d):
            kind = "commute" if name in COMMUTE_LABELS else "class" if name in class_names else "event"
            blocks.append(Block(d, s, e, short.get(name, name), kind, title=name))
    if store is not None:
        for plan in store.plans:
            previous = None  # 바로 이어지는 같은 작업의 세션은 한 블록으로 보여 준다
            for s in sorted(plan["sessions"], key=lambda x: (x["date"], x["start"])):
                d = date.fromisoformat(s["date"])
                if not days[0] <= d <= days[-1]:
                    continue
                title = s.get("title", s["task_id"])
                start, end = to_minutes(s["start"]), to_minutes(s["end"])
                if previous and previous.day == d and previous.end == start and previous.name == title:
                    previous.end = end
                    continue
                previous = Block(d, start, end, title, "plan", title=f"{Path(plan['assignment']).stem} · {title}")
                blocks.append(previous)
    assign_shades(blocks)
    return blocks


def assign_shades(blocks: list[Block]) -> None:
    """날짜·종류별로 시간순 1, 2, 1, 2 … 번갈아 색 농도를 정한다."""
    counters: dict[tuple[date, str], int] = {}
    for b in sorted(blocks, key=lambda b: (b.day, b.start)):
        key = (b.day, b.kind)
        b.shade = counters.get(key, 0) % 2
        counters[key] = counters.get(key, 0) + 1


# ----- 터미널 출력 ------------------------------------------------------------
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _fit(text: str, width: int, center: bool = False) -> str:
    """한글(전각) 폭을 고려해 width에 맞게 자르고 공백으로 채운다. center=True면 가운데 정렬."""
    out, used = "", 0
    for ch in text:
        w = _width(ch)
        if used + w > width:
            break
        out, used = out + ch, used + w
    pad = width - used
    left = pad // 2 if center else 0
    return " " * left + out + " " * (pad - left)


def render_terminal(blocks: list[Block], days: list[date], day_start: int, day_end: int, today: date) -> str:
    priority = list(KINDS)
    lines = []
    header = " " * 6 + "".join(
        (BOLD if d == today else "") + _fit(f"{d.month}/{d.day}({KOR_WEEKDAYS[d.weekday()]})", CELL - 1, center=True)
        + " " + RESET
        for d in days
    )
    lines.append(header)
    shown: dict[date, Block | None] = {d: None for d in days}  # 날짜별로 바로 윗칸에 표시한 블록
    for t in range(day_start, day_end, SLOT):
        row = f"{fmt_minutes(t)} " if t % 60 == 0 else " " * 6
        for d in days:
            here = [b for b in blocks if b.day == d and b.start < t + SLOT and b.end > t]
            if not here:
                shown[d] = None
                dot = _fit("·", CELL - 1, center=True)  # 비어 있는 15분 (오늘은 굵게)
                row += (BOLD + dot + RESET if d == today else dot) + " "
                continue
            b = min(here, key=lambda x: priority.index(x.kind))
            # 블록이 화면에 처음 나타나는 칸에 이름을 쓴다. (앞 일정에 가려 시작 칸이 안 보이는 경우 포함)
            label = b.name if shown[d] is not b else ""
            shown[d] = b
            row += b.terminal_style + _fit(label, CELL - 1, center=True) + RESET + " "
        lines.append(row)
    legend = "  ".join(f"{styles[0]} {name} {RESET}{styles[1]}   {RESET}" for styles, _, name in KINDS.values())
    lines.append("\n범례: " + legend + "   (· = 과제 가능한 빈 15분)")
    return "\n".join(lines)


# ----- HTML 출력 --------------------------------------------------------------
def render_html(blocks: list[Block], days: list[date], day_start: int, day_end: int, today: date) -> str:
    span = day_end - day_start
    hours = "".join(
        f'<div class="hour" style="top:{(m - day_start) / span * 100:.3f}%">{fmt_minutes(m)}</div>'
        for m in range(day_start, day_end, 60)
    )
    cols = []
    for d in days:
        items = []
        for b in sorted((b for b in blocks if b.day == d), key=lambda b: b.start):
            top = (max(b.start, day_start) - day_start) / span * 100
            height = (min(b.end, day_end) - max(b.start, day_start)) / span * 100
            if height <= 0:
                continue
            items.append(
                f'<div class="blk" style="top:{top:.3f}%;height:{height:.3f}%;background:{b.html_color}" '
                f'title="{html.escape(b.title or b.name)} {fmt_minutes(b.start)}-{fmt_minutes(b.end)}">'
                f"<b>{html.escape(b.name)}</b><span>{fmt_minutes(b.start)}-{fmt_minutes(b.end)}</span></div>"
            )
        cls = "day today" if d == today else "day"
        cols.append(
            f'<div class="{cls}"><div class="dh">{d.month}/{d.day} ({KOR_WEEKDAYS[d.weekday()]})</div>'
            f'<div class="body">{"".join(items)}</div></div>'
        )
    legend = "".join(
        f'<span class="lg"><i style="background:{c[0]}"></i><i style="background:{c[1]}"></i>{n}</span>'
        for _, c, n in KINDS.values()
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>주간 시간표</title>
<style>
body{{font-family:system-ui,-apple-system,"Malgun Gothic",sans-serif;margin:24px;color:#1f2328;background:#fff}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;margin-bottom:12px;font-size:13px}}
.grid{{display:grid;grid-template-columns:52px repeat(7,1fr);gap:4px;min-width:760px}}
.hours,.body{{position:relative;height:{(span // 60) * 44}px}}
.hour{{position:absolute;font-size:11px;color:#888;transform:translateY(-6px)}}
.dh{{text-align:center;font-weight:600;font-size:13px;padding:4px 0;border-bottom:1px solid #ddd}}
.day .body{{background:repeating-linear-gradient(#fff 0 43px,#eee 43px 44px)}}
.today .dh{{color:#fff;background:#1f2328;border-radius:6px}}
.blk{{position:absolute;left:2px;right:2px;border-radius:5px;color:#fff;font-size:11px;padding:2px 4px;
overflow:hidden;box-sizing:border-box;line-height:1.25}}
.blk b{{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.blk span{{opacity:.85}}
.legend{{margin-top:12px;font-size:13px}} .lg{{margin-right:14px}}
.lg i{{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:4px;vertical-align:-1px}}
</style></head><body>
<h1>주간 시간표</h1>
<div class="sub">{days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d} · 빈 공간은 과제를 할 수 있는 시간입니다.</div>
<div class="grid"><div><div class="dh">&nbsp;</div><div class="hours">{hours}</div></div>{"".join(cols)}</div>
<div class="legend">{legend}</div>
</body></html>
"""


def show_week(timetable: dict, store: PlanStore | None, target: date, output_dir: Path) -> Path:
    """기준 날짜가 속한 주를 터미널에 출력하고 HTML 파일로 저장한다."""
    days = week_of(target)
    schedule = Schedule(timetable, datetime.combine(days[0], datetime.min.time()))
    blocks = collect_blocks(timetable, store, days)
    print(f"{BOLD}주간 시간표 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d}{RESET}\n")
    print(render_terminal(blocks, days, schedule.day_start, schedule.day_end, target))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"week_{days[0]:%Y%m%d}.html"
    path.write_text(render_html(blocks, days, schedule.day_start, schedule.day_end, target), encoding="utf-8")
    return path
