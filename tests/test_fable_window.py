"""Showing the Max-only Fable 5 weekly limit, out of extraRateWindows.

The "Fable only" entry a codexbar claude payload carries in extraRateWindows
(id=claude-weekly-scoped-fable, seven days) is added as a **display-only line**.
A standard payload has no such entry, so the line is quietly omitted — no error
and no blank row. classify(pick) and the guard stay on the whole weekly figure.

The regression this guards: before it existed, coach ignored Fable completely,
even at 73% used.
"""
import sys, os, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coach


def _iso(mins):
    return (coach.now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


def _usage(extra):
    return {
        "primary":   {"windowMinutes": 300,   "usedPercent": 4,  "resetsAt": _iso(120)},
        "secondary": {"windowMinutes": 10080, "usedPercent": 54, "resetsAt": _iso(1300)},
        "tertiary":  None,
        "extraRateWindows": extra,
    }


FABLE = {"id": "claude-weekly-scoped-fable", "title": "Fable only",
         "window": {"windowMinutes": 10080, "usedPercent": 73, "resetsAt": _iso(1300)}}
ROUTINES = {"id": "claude-routines", "title": "Daily Routines",
            "window": {"windowMinutes": 10080, "usedPercent": 0}}

_fails = []


def _check(name, cond, got=None):
    print(("  PASS" if cond else "  FAIL"), name, ("" if got is None else f"-> {got}"))
    if not cond:
        _fails.append(name)


def main():
    print("[fable_window] a Max subscription (the entry is there) -> a Fable row")
    fw = coach.fable_window(_usage([ROUTINES, FABLE]))
    _check("a row comes back", fw is not None, fw)
    _check("name=Fable, left=27", bool(fw) and fw[0] == "Fable" and fw[1] == 27, fw)

    print("[fable_window] a standard subscription (no entry) -> None, omitted")
    _check("no extraRateWindows key at all", coach.fable_window({"primary": None}) is None)
    _check("extraRateWindows: null", coach.fable_window(_usage(None)) is None)
    _check("an empty array", coach.fable_window(_usage([])) is None)
    _check("Routines alone must not be mistaken for it", coach.fable_window(_usage([ROUTINES])) is None)

    print("[fable_window] an invalid window -> omitted, and no crash")
    bad = {"id": "claude-weekly-scoped-fable", "title": "Fable only",
           "window": {"windowMinutes": 10080, "usedPercent": 73}}   # resetsAt missing while in use
    _check("resetsAt missing", coach.fable_window(_usage([bad])) is None)
    _check("window: null", coach.fable_window(_usage([{"id": "claude-weekly-scoped-fable"}])) is None)

    print("[not part of the decision] pick(7d) stays on the whole week at 46%, not dragged to Fable's 27%")
    _check("pick=0.46", round(coach.pick(_usage([FABLE]), 10080)["left"], 2) == 0.46)

    print("[to_payload] a Fable row -> the fable_7d key, and reset_min null when mins is None")
    row = {"label": "claude", "ok": True, "plan": "Max", "email": None,
           "emoji": "⚪", "action": "a", "detail": "d",
           "windows": [("7d", 46, 1300.0), ("Fable", 27, None)]}
    w = coach.to_payload([row])["providers"]["claude"]["windows"]
    _check("fable_7d is present", "fable_7d" in w, w)
    _check("left_pct=27, reset_min=null",
           w.get("fable_7d") == {"left_pct": 27, "reset_min": None}, w.get("fable_7d"))

    print("[render] the Fable line is drawn, and omitted when absent")
    os.environ["NO_COLOR"] = "1"
    base = dict(row, windows=[("7d", 46, 1300.0)])
    _check("present -> 'Fable' appears", "Fable" in coach.render([row]))
    _check("absent -> 'Fable' does not", "Fable" not in coach.render([base]))

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL -> {_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
