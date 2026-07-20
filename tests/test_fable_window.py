"""Max 구독 전용 Fable 5 주간 한도(extraRateWindows) 표시 검증.

codexbar claude 페이로드의 extraRateWindows에 오는 "Fable only"
(id=claude-weekly-scoped-fable, 7일)를 **표시 전용 라인**으로 추가한다.
기본 구독 페이로드엔 항목이 없다 → 라인 조용히 생략(에러·빈 라인 없음).
classify(pick)·guard 판정은 기존 전체 주간 기준 그대로(미반영).

회귀 방지: 추가 전엔 Fable 73% 사용 중이어도 coach가 완전히 무시했다.
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
    print("[fable_window] Max 구독(항목 있음) → Fable 행 반환")
    fw = coach.fable_window(_usage([ROUTINES, FABLE]))
    _check("행 반환", fw is not None, fw)
    _check("이름=Fable, left=27", bool(fw) and fw[0] == "Fable" and fw[1] == 27, fw)

    print("[fable_window] 기본 구독(항목 없음) → None(생략)")
    _check("extraRateWindows 키 자체 없음", coach.fable_window({"primary": None}) is None)
    _check("extraRateWindows: null", coach.fable_window(_usage(None)) is None)
    _check("빈 배열", coach.fable_window(_usage([])) is None)
    _check("Routines만 있음(오인 금지)", coach.fable_window(_usage([ROUTINES])) is None)

    print("[fable_window] 무효 윈도우 → 생략(크래시 없음)")
    bad = {"id": "claude-weekly-scoped-fable", "title": "Fable only",
           "window": {"windowMinutes": 10080, "usedPercent": 73}}   # resetsAt 누락+사용중
    _check("resetsAt 누락", coach.fable_window(_usage([bad])) is None)
    _check("window: null", coach.fable_window(_usage([{"id": "claude-weekly-scoped-fable"}])) is None)

    print("[판정 미반영] pick(7일)은 전체 주간(46%) 유지 — Fable(27%)에 안 끌려감")
    _check("pick=0.46", round(coach.pick(_usage([FABLE]), 10080)["left"], 2) == 0.46)

    print("[to_payload] Fable 행 -> fable_7d 키, mins None이면 reset_min null")
    row = {"label": "claude", "ok": True, "plan": "Max", "email": None,
           "emoji": "⚪", "action": "a", "detail": "d",
           "windows": [("7일", 46, 1300.0), ("Fable", 27, None)]}
    w = coach.to_payload([row])["providers"]["claude"]["windows"]
    _check("fable_7d 존재", "fable_7d" in w, w)
    _check("left_pct=27, reset_min=null",
           w.get("fable_7d") == {"left_pct": 27, "reset_min": None}, w.get("fable_7d"))

    print("[render] Fable 라인 출력 / 없으면 미출력")
    os.environ["NO_COLOR"] = "1"
    base = dict(row, windows=[("7일", 46, 1300.0)])
    _check("있음 → 'Fable' 포함", "Fable" in coach.render([row]))
    _check("없음 → 'Fable' 미포함", "Fable" not in coach.render([base]))

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL -> {_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
