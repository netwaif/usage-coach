"""Several windows sharing a windowMinutes — seven days, say — consolidate into one.

codexbar reports per-model and per-feature sub-limits against the same seven
days: Claude weekly plus Sonnet weekly plus Daily Routines, or two Antigravity
weeklies. coach consolidates them into **the tightest one** — least left is what
blocks first — so render, to_payload (--json) and classify(pick) all look at the
same window.

The regression this guards: before consolidation, render drew the seven days
twice, --json took the last window and classify the first, so the three
disagreed.
"""
import sys, os, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coach


def _iso(mins):
    return (coach.now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


# Claude: a 5h session, weekly (left 92, used 8), and Sonnet weekly (left 99, used 1)
CLAUDE = {
    "primary":   {"windowMinutes": 300,   "usedPercent": 11, "resetsAt": _iso(280)},
    "secondary": {"windowMinutes": 10080, "usedPercent": 8,  "resetsAt": _iso(2420)},
    "tertiary":  {"windowMinutes": 10080, "usedPercent": 1,  "resetsAt": _iso(2420)},
}
# Antigravity: weekly (left 67, used 33) and an unused weekly (left 100, used 0)
ANTIGRAV = {
    "primary":   {"windowMinutes": 10080, "usedPercent": 33, "resetsAt": _iso(9900)},
    "secondary": {"windowMinutes": 10080, "usedPercent": 0,  "resetsAt": _iso(10000)},
    "tertiary":  None,
}

WK = coach.WIN_NAME[10080]   # "7d"
_fails = []


def _check(name, cond, got=None):
    print(("  PASS" if cond else "  FAIL"), name, ("" if got is None else f"-> {got}"))
    if not cond:
        _fails.append(name)


def main():
    print("[windows_summary] seven days collapses to the tightest single row")
    cs = [r for r in coach.windows_summary(CLAUDE) if r[0] == WK]
    _check("claude has one 7d row", len(cs) == 1, cs)
    _check("claude 7d = 92, the weekly one, the smallest", bool(cs) and cs[0][1] == 92, cs)

    ag = [r for r in coach.windows_summary(ANTIGRAV) if r[0] == WK]
    _check("antigravity has one 7d row", len(ag) == 1, ag)
    _check("antigravity 7d = 67, the smallest", bool(ag) and ag[0][1] == 67, ag)

    print("[pick] 7d is the tightest, the smallest left")
    _check("claude pick(10080)=0.92", round(coach.pick(CLAUDE, 10080)["left"], 2) == 0.92)
    _check("antigravity pick(10080)=0.67", round(coach.pick(ANTIGRAV, 10080)["left"], 2) == 0.67)

    print("[consistency] windows_summary's 7d == pick's 7d")
    _check("claude agrees", bool(cs) and cs[0][1] == round(coach.pick(CLAUDE, 10080)["left"] * 100))
    _check("antigravity agrees", bool(ag) and ag[0][1] == round(coach.pick(ANTIGRAV, 10080)["left"] * 100))

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL -> {_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
