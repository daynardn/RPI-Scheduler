#!/usr/bin/env python3
"""
courses.py - RPI schedule builder TUI.

    python3 courses.py schedule.scm              # load/save your plan in this file
    python3 courses.py schedule.scm --refresh    # re-download (seat counts change!)
    python3 courses.py --url zfs202701.htm       # parse a saved local copy instead
    python3 courses.py --allow-full              # treat full sections as pickable

You add courses (and optionally pin a section). The script brute-forces every
combination of open sections, throws out time conflicts, and shows the best
one: fewest days on campus, then smallest gaps between classes.

deps:  pip install questionary rich
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import questionary
from questionary import Choice, Style
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

DEFAULT_URL = "https://sis-sso.rpi.edu/reg/zfs202701.htm"
CACHE_DIR = Path.home() / ".cache" / "rpi-scheduler"
DAYS = "MTWRFSU"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LIMIT = 20000  # stop enumerating after this many valid schedules
PALETTE = ["#8ecae6", "#ffb703", "#b7e4c7", "#f4a6c0", "#cdb4db", "#ffd6a5", "#a0c4ff", "#fdffb6"]
STYLE = Style([("disabled", "fg:#6c6c6c strike")])
BACK = "__back__"
CLEAR = "__clear__"
console = Console()


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Meeting:
    kind: str           # LEC / LAB / REC / TES / STU ...
    days: list[int]     # 0=Mon .. 6=Sun
    start: int          # minutes since midnight
    end: int

    def overlaps(self, o: "Meeting") -> bool:
        return bool(set(self.days) & set(o.days)) and self.start < o.end and o.start < self.end


@dataclass
class Section:
    crn: str
    course: str         # "ECSE-2010"
    sec: str            # "01"
    title: str
    credits: str
    instructor: str
    max: int | None
    enrl: int | None
    rem: int | None
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.rem is None or self.rem > 0

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        d = dict(d)
        d["meetings"] = [Meeting(**m) for m in d["meetings"]]
        return cls(**d)


def pretty(code: str) -> str:
    return code.replace("-", " ")


def fmt_time(m: int) -> str:
    h = m // 60
    return f"{h % 12 or 12}:{m % 60:02d}{'AM' if h < 12 else 'PM'}"


def fmt_short(m: int) -> str:
    h = m // 60
    return f"{h % 12 or 12}:{m % 60:02d}{'a' if h < 12 else 'p'}"


def meetings_str(s: Section) -> str:
    if not s.meetings:
        return "TBA"
    return ", ".join(
        f"{m.kind + ' ' if m.kind else ''}{''.join(DAYS[d] for d in m.days)} {fmt_time(m.start)}-{fmt_time(m.end)}"
        for m in s.meetings
    )


# --------------------------------------------------------------------------- #
# scraping / parsing
# --------------------------------------------------------------------------- #
TYPES = "LEC|LAB|REC|TES|STU|SEM|SMR"
SECTION_RE = re.compile(r"(?<![\w-])(\d{5})\s+([A-Z]{4})-(\d{4})-(\w+)(?!\w)")
MEET_RE = re.compile(
    rf"(?<![A-Za-z0-9*])(?:(?P<kind>{TYPES})\s+)?"
    r"(?P<days>[MTWRFSU]+(?:\s+[MTWRFSU]+){0,2})\s+"
    r"(?P<s>\d{1,2}):(?P<sm>\d{2})\s+(?P<e>\d{1,2}):(?P<em>\d{2})\s*(?P<ap>AM|PM)\b"
)
SEAT_RE = re.compile(r"(\d+)\s+(\d+)\s+(-?\d+)\s*$")
TBA_RE = re.compile(r"\*\*\s*TBA\s*\*\*")
VIEW_RE = re.compile(r"View\s*TextBooks")


def to_minutes(s, sm, e, em, ap) -> tuple[int, int]:
    """The page only gives AM/PM on the end time ("9:00  11:50AM"), so infer the start."""
    end = int(e) % 12 * 60 + int(em) + (720 if ap == "PM" else 0)
    h = int(s) % 12 * 60 + int(sm)
    cands = [c for c in (h, h + 720) if c < end]
    return (max(cands) if cands else h), end


def parse_chunk(m: re.Match, chunk: str) -> Section:
    crn, dept, num, sec = m.groups()
    view = VIEW_RE.search(chunk)
    row_end = view.start() if view else len(chunk)
    row1 = chunk[:row_end]
    found = list(MEET_RE.finditer(chunk))
    tba = TBA_RE.search(chunk)

    # everything before the first meeting / TBA marker is "title type credits [gr tp]"
    cuts = ([found[0].start()] if found else []) + ([tba.start()] if tba else [])
    head = chunk[: min(cuts)] if cuts else chunk
    k = re.search(rf"\b({TYPES})\b\s+(?:\S+\s+)?\d+(?:-\d+)?\s*(?:[A-Z]{{1,2}}\s*)?$", head)
    kind0 = k.group(1) if k else ""
    nums = list(re.finditer(r"(?<=\s)\d+(?:-\d+)?(?=\s|$)", head))
    credits = nums[-1].group() if nums else ""
    title = head[: nums[-1].start()] if nums else head
    title = re.sub(rf"\s*\b(?:{TYPES})\b\s*$", "", title.strip()).strip()

    meetings = []
    for mm in found:
        toks = mm["days"].split()
        if toks[0] == "SU":          # "SU" is the satisfactory/unsatisfactory grade column
            toks = toks[1:]
        if not toks:
            continue
        days = sorted({DAYS.index(c) for c in "".join(toks)})
        start, end = to_minutes(mm["s"], mm["sm"], mm["e"], mm["em"], mm["ap"])
        kind = mm["kind"] or (kind0 if mm.start() < row_end else "")
        meetings.append(Meeting(kind, days, start, end))

    mx = en = rem = None
    instr = ""
    if view and (sm := SEAT_RE.search(row1)):
        mx, en, rem = map(int, sm.groups())
        pre = row1[: sm.start()]
        idx = max([mm.end() for mm in found if mm.end() <= len(pre)] + [t.end() for t in TBA_RE.finditer(pre)] + [0])
        instr = pre[idx:].strip()
    return Section(crn, f"{dept}-{num}", sec, title, credits, instr, mx, en, rem, meetings)


def parse_sections(raw: str) -> list[Section]:
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    starts = list(SECTION_RE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        s = parse_chunk(m, text[m.end():end])
        if s.crn not in seen:
            seen.add(s.crn)
            out.append(s)
    return out


def load_sections(url: str, refresh: bool) -> tuple[list[Section], str]:
    local = Path(url)
    if local.exists():
        return parse_sections(local.read_text(errors="replace")), "local file"
    cache = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:10] + ".json")
    if cache.exists() and not refresh:
        d = json.loads(cache.read_text())
        return [Section.from_dict(s) for s in d["sections"]], time.strftime("%Y-%m-%d %H:%M", time.localtime(d["fetched"]))
    console.print(f"[dim]Downloading {url} ...[/]")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (rpi-scheduler)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    secs = parse_sections(raw)
    if not secs:
        sys.exit("Couldn't find any sections on that page - has the format changed?")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetched": time.time(), "sections": [asdict(s) for s in secs]}))
    return secs, "just now"


# --------------------------------------------------------------------------- #
# solver
# --------------------------------------------------------------------------- #
def score(secs) -> tuple[int, int]:
    """(days on campus, total idle minutes between classes) - lower is better."""
    byday: dict[int, list] = {}
    for s in secs:
        for m in s.meetings:
            for d in m.days:
                byday.setdefault(d, []).append((m.start, m.end))
    gaps = 0
    for iv in byday.values():
        iv.sort()
        gaps += sum(b[0] - a[1] for a, b in zip(iv, iv[1:]))
    return len(byday), gaps


class Catalog:
    def __init__(self, sections: list[Section], allow_full: bool = False):
        self.by_course: dict[str, list[Section]] = {}
        for s in sections:
            self.by_course.setdefault(s.course, []).append(s)
        self.allow_full = allow_full
        self._conf: dict[tuple[str, str], bool] = {}

    def pickable(self, s: Section) -> bool:
        return self.allow_full or s.open

    def conflict(self, a: Section, b: Section) -> bool:
        key = (a.crn, b.crn)
        if key not in self._conf:
            r = any(x.overlaps(y) for x in a.meetings for y in b.meetings)
            self._conf[key] = self._conf[(b.crn, a.crn)] = r
        return self._conf[key]

    def options(self, code: str, pin: str | None) -> list[Section]:
        return [s for s in self.by_course[code] if self.pickable(s) and (pin is None or s.crn == pin)]

    def solve(self, plan: dict[str, str | None], limit: int = LIMIT):
        """-> (best {course: Section} or None, number of valid schedules found)"""
        codes = list(plan)
        opts = {c: self.options(c, plan[c]) for c in codes}
        if any(not o for o in opts.values()):
            return None, 0
        order = sorted(codes, key=lambda c: len(opts[c]))
        chosen: dict[str, Section] = {}
        best, count = None, 0

        def rec(k: int):
            nonlocal best, count
            if count >= limit:
                return
            if k == len(order):
                count += 1
                sc = score(chosen.values())
                if best is None or sc < best[0]:
                    best = (sc, dict(chosen))
                return
            c = order[k]
            for s in opts[c]:
                if all(not self.conflict(s, t) for t in chosen.values()):
                    chosen[c] = s
                    rec(k + 1)
                    del chosen[c]

        rec(0)
        return ({c: best[1][c] for c in codes} if best else None), count

    def feasible(self, plan) -> bool:
        return self.solve(plan, 1)[0] is not None

    def explain(self, plan) -> list[str]:
        lines = []
        for c, p in plan.items():
            if not self.options(c, p):
                lines.append(f"{pretty(c)}: " + ("the pinned section is full" if p else "no open sections"))
        if lines:
            return lines
        blockers = [c for c in plan if self.feasible({k: v for k, v in plan.items() if k != c})]
        if blockers:
            return ["Dropping any one of these would fix it: " + ", ".join(pretty(b) for b in blockers)]
        return ["No combination of sections avoids a conflict (3+ way clash)."]

    def resolve(self, text: str) -> str | None:
        """'ecse 2010' / 'ECSE-2010' / 'ecse20' -> 'ECSE-2010' (asks if ambiguous)."""
        norm = re.sub(r"[^A-Z0-9]", "", text.upper())
        if not norm:
            return None
        keys = {c.replace("-", ""): c for c in self.by_course}
        if norm in keys:
            return keys[norm]
        hits = sorted(c for k, c in keys.items() if k.startswith(norm))
        if len(hits) == 1:
            return hits[0]
        if 1 < len(hits) <= 25:
            pick = questionary.select(
                "Which one?", style=STYLE,
                choices=[Choice(f"{pretty(c)}  {self.by_course[c][0].title}", c) for c in hits] + [Choice("back", BACK)],
            ).ask()
            return None if pick in (None, BACK) else pick
        return None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_list(cat: Catalog, plan, assign) -> Table:
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, header_style="bold")
    for col in ("Course", "Title", "Sec", "CRN", "Instructor", "Meetings", "Pin"):
        t.add_column(col, overflow="fold")
    for i, (code, pin) in enumerate(plan.items()):
        color = PALETTE[i % len(PALETTE)]
        s = assign[code] if assign else None
        t.add_row(
            Text(pretty(code), style=f"bold {color}"), cat.by_course[code][0].title,
            s.sec if s else "-", s.crn if s else "-", (s.instructor or "-") if s else "-",
            meetings_str(s) if s else "-", "[b]pinned[/]" if pin else "",
        )
    return t


def render_grid(assign: dict[str, Section]) -> Table | None:
    meets = [m for s in assign.values() for m in s.meetings]
    if not meets:
        return None
    lo = min(m.start for m in meets) // 60 * 60
    hi = -(-max(m.end for m in meets) // 60) * 60
    days = list(range(5)) + [d for d in (5, 6) if any(d in m.days for m in meets)]
    grid: dict[tuple[int, int], tuple[str, str]] = {}
    for i, (code, s) in enumerate(assign.items()):
        color = PALETTE[i % len(PALETTE)]
        for m in s.meetings:
            labels = [f"{pretty(code)} {m.kind}".strip(), f"{fmt_short(m.start)}-{fmt_short(m.end)}"]
            for d in m.days:
                for j, t in enumerate(x for x in range(lo, hi, 30) if x < m.end and x + 30 > m.start):
                    grid[(d, t)] = (labels[j] if j < 2 else "", color)
    tbl = Table(box=box.SIMPLE_HEAD, pad_edge=False, padding=(0, 1))
    tbl.add_column("", justify="right", style="dim")
    for d in days:
        tbl.add_column(DAY_NAMES[d], min_width=15, no_wrap=True)
    for t in range(lo, hi, 30):
        row = [fmt_time(t).replace(":00", "") if t % 60 == 0 else ""]
        for d in days:
            lab, color = grid.get((d, t), (None, None))
            row.append(Text(f" {lab}".ljust(15)[:15], style=f"black on {color}") if color else "")
        tbl.add_row(*row)
    return tbl


def describe(s: Section) -> str:
    seats = f"{s.rem}/{s.max} open" if s.rem is not None else "seats ?"
    return f"{s.sec:>3}  CRN {s.crn}  {(s.instructor or 'Staff')[:16]:<16} {meetings_str(s)}  [{seats}]"


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, cat: Catalog, url: str, path: str | None):
        self.cat, self.url, self.path = cat, url, path
        self.plan: dict[str, str | None] = {}
        self.dirty = False
        self.notice = ""

    # persistence ----------------------------------------------------------
    def load(self):
        d = json.loads(Path(self.path).read_text())
        for c in d.get("courses", []):
            if c["course"] in self.cat.by_course:
                self.plan[c["course"]] = c.get("pin")
            else:
                self.notice += f"[yellow]Skipped {c['course']}: not in this term's listing.[/]\n"

    def save(self, path: str):
        data = {"url": self.url, "courses": [{"course": c, "pin": p} for c, p in self.plan.items()]}
        Path(path).write_text(json.dumps(data, indent=2) + "\n")
        self.path, self.dirty = path, False
        console.print(f"[green]Saved to {path}[/]")

    # flows ----------------------------------------------------------------
    def add(self):
        names = {pretty(c): self.cat.by_course[c][0].title for c in sorted(self.cat.by_course)}
        ans = questionary.autocomplete(
            "What course would you like to add (enter with no input to go back):",
            choices=list(names), meta_information=names, match_middle=True, ignore_case=True,
            style=STYLE, qmark=">",
        ).ask()
        if not ans or not ans.strip():
            return
        code = self.cat.resolve(ans)
        if not code:
            self.notice = f"[red]No course matching '{ans.strip()}'.[/]"
        elif code in self.plan:
            self.notice = f"[yellow]{pretty(code)} is already in your list.[/]"
        else:
            new = {**self.plan, code: None}
            if self.cat.solve(new)[0] is None:
                self.notice = f"[red]Can't add {pretty(code)}: no conflict-free section exists.[/]\n" + "\n".join(
                    "  " + line for line in self.cat.explain(new))
            else:
                self.plan, self.dirty = new, True

    def pin(self):
        if not self.plan:
            self.notice = "[yellow]Add a course first.[/]"
            return
        code = questionary.select(
            "Pin which course?", style=STYLE, qmark=">",
            choices=[Choice(f"{pretty(c)}  {self.cat.by_course[c][0].title}" + (f"   (pinned: {p})" if p else ""), c)
                     for c, p in self.plan.items()] + [Choice("back", BACK)],
        ).ask()
        if code in (None, BACK):
            return
        choices = []
        for s in sorted(self.cat.by_course[code], key=lambda s: s.sec):
            if not self.cat.pickable(s):
                why = "full"
            elif not self.cat.feasible({**self.plan, code: s.crn}):
                why = "conflicts with your other courses"
            else:
                why = None
            choices.append(Choice(describe(s), s.crn, disabled=why))
        choices += [Choice("clear pin (let it auto-pick)", CLEAR), Choice("back", BACK)]
        pick = questionary.select(f"Select a section of {pretty(code)} to pin:", choices=choices, style=STYLE, qmark=">").ask()
        if pick in (None, BACK):
            return
        self.plan[code] = None if pick == CLEAR else pick
        self.dirty = True

    def remove(self):
        if not self.plan:
            self.notice = "[yellow]Nothing to remove.[/]"
            return
        code = questionary.select(
            "Remove which course?", style=STYLE, qmark=">",
            choices=[Choice(f"{pretty(c)}  {self.cat.by_course[c][0].title}", c) for c in self.plan] + [Choice("back", BACK)],
        ).ask()
        if code not in (None, BACK):
            del self.plan[code]
            self.dirty = True

    def quit(self):
        if self.path:
            if (self.dirty or not Path(self.path).exists()) and questionary.confirm(
                    f"Would you like to save to {self.path}?", default=True, style=STYLE).ask():
                self.save(self.path)
        elif self.plan:
            p = questionary.text("Save as (leave blank to discard):", style=STYLE).ask()
            if p and p.strip():
                self.save(p.strip())

    # main loop ------------------------------------------------------------
    def draw(self):
        console.clear()
        console.rule("[bold]RPI schedule builder")
        console.print("[dim]Courses[/]")
        if not self.plan:
            console.print("  (none yet)")
            return
        assign, n = self.cat.solve(self.plan)
        console.print(render_list(self.cat, self.plan, assign))
        console.rule()
        if assign is None:
            console.print("[bold red]No course schedule can be generated.[/]")
            for line in self.cat.explain(self.plan):
                console.print("  " + line)
        else:
            grid = render_grid(assign)
            console.print(grid if grid else "[dim]All of these courses are TBA - nothing to draw.[/]")
            more = "+" if n >= LIMIT else ""
            console.print(f"[dim]{n}{more} conflict-free combination(s); showing the one with the fewest days/gaps.[/]")
        console.rule()

    def run(self):
        menu = [Choice("add a course", "add"), Choice("pin a course (pick a specific section)", "pin"),
                Choice("remove a course", "remove"), Choice("quit", "quit")]
        while True:
            self.draw()
            if self.notice:
                console.print(self.notice)
                self.notice = ""
            act = questionary.select("", choices=menu, style=STYLE, qmark="").ask() or "quit"
            if act == "quit":
                return self.quit()
            getattr(self, act)()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="schedule file to load from / save to (created on first save)")
    ap.add_argument("--url", default=DEFAULT_URL, help="course listing URL (or a saved .htm file)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    ap.add_argument("--allow-full", action="store_true", help="let full sections be auto-picked / pinned")
    args = ap.parse_args()

    sections, fetched = load_sections(args.url, args.refresh)
    cat = Catalog(sections, args.allow_full)
    console.print(f"[dim]{len(sections)} sections / {len(cat.by_course)} courses (data from {fetched})[/]")
    app = App(cat, args.url, args.path)
    if args.path and Path(args.path).exists():
        app.load()
    try:
        app.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
