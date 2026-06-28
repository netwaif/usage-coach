"""같은 windowMinutes(예: 7일) 윈도우가 여러 개일 때 한 개로 통합되는지 검증.

codexbar는 모델·기능별 하위 한도를 같은 7일로 여러 개 준다
(예: Claude 주간 + Sonnet 주간 + Daily Routines, Antigravity 주간 2개).
coach는 이를 **가장 빡빡한(남은 % 최소 = 먼저 막히는 병목) 1개**로 통합해
render · to_payload(--json) · classify(pick)가 모두 같은 윈도우를 보게 한다.

회귀 방지: 통합 전엔 render가 7일 두 줄(중복)을 그리고, --json은 마지막 윈도우를,
classify는 첫 윈도우를 써서 서로 달랐다.
"""
import sys, os, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coach


def _iso(mins):
    return (coach.now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


# Claude: 5h 세션 + 주간(left92,used8) + Sonnet 주간(left99,used1)
CLAUDE = {
    "primary":   {"windowMinutes": 300,   "usedPercent": 11, "resetsAt": _iso(280)},
    "secondary": {"windowMinutes": 10080, "usedPercent": 8,  "resetsAt": _iso(2420)},
    "tertiary":  {"windowMinutes": 10080, "usedPercent": 1,  "resetsAt": _iso(2420)},
}
# Antigravity: 주간(left67,used33) + 안 쓴 주간(left100,used0)
ANTIGRAV = {
    "primary":   {"windowMinutes": 10080, "usedPercent": 33, "resetsAt": _iso(9900)},
    "secondary": {"windowMinutes": 10080, "usedPercent": 0,  "resetsAt": _iso(10000)},
    "tertiary":  None,
}

WK = coach.WIN_NAME[10080]   # "7일"
_fails = []


def _check(name, cond, got=None):
    print(("  PASS" if cond else "  FAIL"), name, ("" if got is None else f"-> {got}"))
    if not cond:
        _fails.append(name)


def main():
    print("[windows_summary] 같은 7일은 가장 빡빡한 1행만")
    cs = [r for r in coach.windows_summary(CLAUDE) if r[0] == WK]
    _check("claude 7일 1행", len(cs) == 1, cs)
    _check("claude 7일 = 92(주간, 최소)", bool(cs) and cs[0][1] == 92, cs)

    ag = [r for r in coach.windows_summary(ANTIGRAV) if r[0] == WK]
    _check("antigravity 7일 1행", len(ag) == 1, ag)
    _check("antigravity 7일 = 67(최소)", bool(ag) and ag[0][1] == 67, ag)

    print("[pick] 7일 = 가장 빡빡한(최소 left)")
    _check("claude pick(10080)=0.92", round(coach.pick(CLAUDE, 10080)["left"], 2) == 0.92)
    _check("antigravity pick(10080)=0.67", round(coach.pick(ANTIGRAV, 10080)["left"], 2) == 0.67)

    print("[일관성] windows_summary 7일 == pick 7일")
    _check("claude 일관", bool(cs) and cs[0][1] == round(coach.pick(CLAUDE, 10080)["left"] * 100))
    _check("antigravity 일관", bool(ag) and ag[0][1] == round(coach.pick(ANTIGRAV, 10080)["left"] * 100))

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL -> {_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
