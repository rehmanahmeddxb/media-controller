#!/usr/bin/env python3
"""
PLAN.md progress tracker for Ahmed Reaction Studio.

Usage
-----
    python3 scripts/update_plan_progress.py                 # recompute the progress dashboard
    python3 scripts/update_plan_progress.py done P3-07      # mark a task complete (+ stamp register)
    python3 scripts/update_plan_progress.py done P3-07 -n "verified on Windows"
    python3 scripts/update_plan_progress.py undone P3-07    # revert a task
    python3 scripts/update_plan_progress.py status          # print counts to stdout only

Marking completion:
    done ID    -> flips "- [ ] **ID**" to "- [x] **ID**" and appends
                  "ID - <date> - note" under the matching section of the
                  Task Status Register (Appendix F).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent / "PLAN.md"

CHECKBOX = re.compile(r"^(?P<indent>\s*)- \[(?P<state>[ xX~!])\]\s+(?P<body>.*)$")
ID_IN_BODY = re.compile(r"\*\*(?P<id>[A-Z]{1,3}\d*-[A-Z]*\d+)\*\*")
GROUP_HEADING = re.compile(r"^#\s+(?P<title>.+?)\s*$")

START = "<!-- PROGRESS:START -->"
END = "<!-- PROGRESS:END -->"
R_START = "<!-- REGISTER:START -->"
R_END = "<!-- REGISTER:END -->"

DONE_STATES = {"x", "X"}
BAR_WIDTH = 20


def _bar(pct: float) -> str:
    filled = int(round(BAR_WIDTH * pct / 100.0))
    filled = max(0, min(BAR_WIDTH, filled))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def scan(lines: list[str]) -> tuple[list[tuple[str, int, int]], dict[str, tuple[int, str]]]:
    """Return ([(group, done, total)], {task_id: (line_index, state)})."""
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    tasks: dict[str, tuple[int, str]] = {}
    current: str | None = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n")
        heading = GROUP_HEADING.match(line)
        if heading:
            current = heading.group("title").strip()
            if current not in groups:
                groups[current] = [0, 0]
                order.append(current)
            continue
        if current is None:
            continue
        m = CHECKBOX.match(line)
        if not m:
            continue
        groups[current][1] += 1
        state = m.group("state")
        if state in DONE_STATES:
            groups[current][0] += 1
        idm = ID_IN_BODY.search(m.group("body"))
        if idm:
            tasks[idm.group("id")] = (idx, state)

    return [(g, groups[g][0], groups[g][1]) for g in order if groups[g][1]], tasks


def _rank(task_id: str) -> tuple[int, int, int]:
    """Sort P-tasks first (by phase), then the sign-off checklists."""
    m = re.match(r"^P(\d+)-E(\d+)$", task_id)
    if m:
        return (0, int(m.group(1)), 1000 + int(m.group(2)))
    m = re.match(r"^P(\d+)-(\d+)$", task_id)
    if m:
        return (0, int(m.group(1)), int(m.group(2)))
    return {"GR": (1, 0, 0), "AC": (2, 0, 0), "NN": (3, 0, 0)}.get(
        task_id.split("-")[0], (4, 0, 0)
    )


def render_dashboard(rows: list[tuple[str, int, int]], next_up: list[str]) -> str:
    total_done = sum(d for _, d, _ in rows)
    total_all = sum(t for _, _, t in rows)
    pct = (100.0 * total_done / total_all) if total_all else 0.0

    out: list[str] = []
    out.append(f"### Overall: **{total_done} / {total_all}** tasks complete (**{pct:.1f}%**)")
    out.append("")
    out.append(f"`[{_bar(pct)}]`")
    out.append("")
    out.append("| Group | Done | Total | % | Progress |")
    out.append("|---|---:|---:|---:|---|")
    for name, done, total in rows:
        gp = (100.0 * done / total) if total else 0.0
        mark = " ✅" if total and done == total else ""
        out.append(f"| {name}{mark} | {done} | {total} | {gp:.0f}% | `{_bar(gp)}` |")
    out.append("")
    if next_up:
        out.append("**Next up:** " + ", ".join(f"`{t}`" for t in next_up[:5]))
        out.append("")
    out.append(f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(out)


def replace_block(text: str, start: str, end: str, body: str) -> str:
    try:
        i = text.index(start)
        j = text.index(end, i)
    except ValueError as exc:  # pragma: no cover
        raise SystemExit(f"Marker {start}/{end} not found in {PLAN}") from exc
    return f"{text[:i]}{start}\n{body}\n{text[j:]}"


def refresh(path: Path = PLAN) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    rows, tasks = scan(lines)
    pending = [tid for tid, (_, st) in tasks.items() if st not in DONE_STATES]
    pending.sort(key=_rank)
    body = render_dashboard(rows, pending)
    text = replace_block(text, START, END, body)
    path.write_text(text, encoding="utf-8")
    done = sum(d for _, d, _ in rows)
    total = sum(t for _, _, t in rows)
    return done, total


def register_section(task_id: str) -> str:
    if task_id.startswith("GR-"):
        return "Ground rules"
    if task_id.startswith(("AC-", "NN-")):
        return "Appendices"
    m = re.match(r"^P(\d+)-", task_id)
    if m:
        return f"Phase {int(m.group(1))}"
    return "Appendices"


def _register_lines(text: str) -> tuple[int, int, list[str]]:
    i, j = text.index(R_START), text.index(R_END)
    block = text[i + len(R_START): j]
    lines = [ln for ln in block.split("\n") if ln.strip()]
    return i, j, lines


def _write_register(text: str, i: int, j: int, lines: list[str]) -> str:
    return text[: i + len(R_START)] + "\n" + "\n".join(lines) + "\n" + text[j:]


def stamp_register(text: str, task_id: str, note: str, date: str) -> str:
    section = register_section(task_id)
    entry = f"  - {task_id} — ✅ {date}" + (f" — {note}" if note else "")
    i, j, lines = _register_lines(text)

    header = f"_{section}_"
    entry_re = re.compile(r"^\s*-\s+" + re.escape(task_id) + r"\s+—")

    # drop any existing entry for this id
    lines = [ln for ln in lines if not entry_re.match(ln)]

    if header not in [ln.strip() for ln in lines]:
        lines.append(header)
    hi = next(k for k, ln in enumerate(lines) if ln.strip() == header)

    # insert after existing entries belonging to this section (or right after header)
    k = hi + 1
    while k < len(lines) and (lines[k].startswith("  - ") or lines[k].strip() == ""):
        if lines[k].startswith("  - "):
            k += 1
        elif any(l.startswith("  - ") for l in lines[k + 1:]):
            break
        else:
            k += 1
    lines.insert(k, entry)
    return _write_register(text, i, j, lines)


def unstamp_register(text: str, task_id: str) -> str:
    i, j, lines = _register_lines(text)
    entry_re = re.compile(r"^\s*-\s+" + re.escape(task_id) + r"\s+—")
    lines = [ln for ln in lines if not entry_re.match(ln)]
    return _write_register(text, i, j, lines)


def set_state(task_id: str, done: bool, note: str = "") -> None:
    text = PLAN.read_text(encoding="utf-8")
    lines = text.split("\n")
    _, tasks = scan([ln + "\n" for ln in lines])
    if task_id not in tasks:
        raise SystemExit(f"Task id {task_id!r} not found in {PLAN}")
    idx, state = tasks[task_id]
    if done and state in DONE_STATES:
        print(f"{task_id} is already complete.")
        return
    if not done and state not in DONE_STATES:
        print(f"{task_id} is not marked complete.")
        return

    new_state = "x" if done else " "
    body = lines[idx]
    body = CHECKBOX.sub(lambda m: f"{m.group('indent')}- [{new_state}] {m.group('body')}", body, count=1)
    lines[idx] = body
    text = "\n".join(lines)
    text = stamp_register(text, task_id, note, datetime.now().strftime("%Y-%m-%d")) if done \
        else unstamp_register(text, task_id)
    PLAN.write_text(text, encoding="utf-8")
    refresh()
    print(f"{task_id} marked {'COMPLETE' if done else 'not complete'}.")


def print_status() -> None:
    lines = PLAN.read_text(encoding="utf-8").splitlines(keepends=True)
    rows, tasks = scan(lines)
    for name, done, total in rows:
        pct = (100.0 * done / total) if total else 0.0
        print(f"{name:<28} {done:>4}/{total:<4} {pct:5.1f}%")
    d = sum(x for _, x, _ in rows)
    t = sum(x for _, _, x in rows)
    print(f"{'TOTAL':<28} {d:>4}/{t:<4} {(100.0*d/t) if t else 0:5.1f}%")
    print(f"tracked ids: {len(tasks)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Track PLAN.md completion progress.")
    ap.add_argument("command", nargs="?", default="refresh",
                    choices=["refresh", "status", "done", "undone"])
    ap.add_argument("task_id", nargs="?", help="task id, e.g. P3-07")
    ap.add_argument("-n", "--note", default="", help="note written to the status register")
    args = ap.parse_args()

    if args.command in ("done", "undone") and not args.task_id:
        ap.error(f"{args.command} requires a task id")

    if args.command == "status":
        print_status()
    elif args.command == "refresh":
        d, t = refresh()
        print(f"PLAN.md progress refreshed: {d}/{t}")
    elif args.command == "done":
        set_state(args.task_id.upper(), True, args.note)
    else:
        set_state(args.task_id.upper(), False, args.note)


if __name__ == "__main__":
    sys.exit(main())
