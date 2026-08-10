"""2부 요금가드(guard_check / load_guard_config / guard_enabled) 회귀 테스트.

guard_check는 7일 윈도우에 대한 하이브리드 OR 가드다:
  left < timeLeft - m   (pace — pace:false면 건너뜀)
  OR left < floor       (absolute — 바닥 임박)
둘 중 하나라도 걸리면 정지(사유 문자열), 아니면 None(통과).

검증 항목:
  (a) 하이브리드 OR — pace만·floor만 각각 걸려도 정지, 둘 다 아니면 통과
  (b) pace=false → 페이스 조건 건너뛰고 floor만 검사
  (c) fail-open — 데이터 오류/결측/조회 예외/7일 윈도우 부재 시 통과
  (d) provider별 m/floor 설정이 load_guard_config(파일) → guard_check(판정)까지 실제로 적용
  (+) guard_enabled — 플래그 파일 존재 = on / 부재 = off

스타일은 test_window_consolidation.py 에 맞춤: pytest 아님, 실행형 self-check
(_check 헬퍼 + main() + sys.exit(main())). coach.py 는 건드리지 않는다.
"""
import sys
import os
import json
import shutil
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coach

_fails = []
_tmpfiles = []   # 임시 usage/config JSON — 종료 전 삭제
_tmpdirs = []


def _check(name, cond, got=None):
    print(("  PASS" if cond else "  FAIL"), name, ("" if got is None else f"-> {got}"))
    if not cond:
        _fails.append(name)


def _iso(mins):
    return (coach.now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


def _win(left, time_left_frac, wm=10080):
    """left=남은 비율, time_left_frac=윈도우 시간 중 남은 비율 → codexbar window dict.
    resetsAt를 now 기준 (time_left_frac * wm) 분 뒤로 줘 win() 의 timeLeft 가 frac 이 되게."""
    mins = time_left_frac * wm
    resets = (coach.now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")
    return {"windowMinutes": wm, "usedPercent": round((1 - left) * 100), "resetsAt": resets}


def _usage(*windows):
    """window dict 들을 primary/secondary/tertiary usage 로 포장(빈 자리는 None)."""
    keys = ("primary", "secondary", "tertiary")
    return {keys[i]: (windows[i] if i < len(windows) else None) for i in range(3)}


def _new_tmp(suffix=".json"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    _tmpfiles.append(path)
    return path


def _src(usage, label="claude"):
    """usage dict 를 임시 JSON 파일로 쓰고 (label, 'file', path) 소스 반환.
    guard_check 가 load_usage('file') → [0]['usage'] 로 꺼내므로 이 포맷이어야 한다."""
    path = _new_tmp()
    with open(path, "w") as f:
        json.dump([{"usage": usage}], f)
    return (label, "file", path)


def _cfg_file(obj=None, raw=None):
    """guard.json 처럼 쓴 임시 파일 경로. obj 면 JSON 직렬화, raw 면 그대로(broken/non-dict 용)."""
    path = _new_tmp()
    with open(path, "w") as f:
        f.write(json.dumps(obj) if raw is None else raw)
    return path


def _cleanup():
    for p in _tmpfiles:
        try:
            os.unlink(p)
        except OSError:
            pass
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)


def main():
    # ── (a) 하이브리드 OR: pace·floor 중 하나만 걸려도 정지 ──────────────
    print("[하이브리드 OR] pace·floor 중 하나만 걸려도 정지")
    pace_src  = _src(_usage(_win(0.30, 0.50)))   # left30% / timeLeft0.50 → pace(0.30<0.40), floor 아님
    floor_src = _src(_usage(_win(0.05, 0.10)))   # left5%  / timeLeft0.10 → floor(0.05<0.10), pace 아님
    clear_src = _src(_usage(_win(0.50, 0.40)))   # left50% / timeLeft0.40 → 둘 다 아님

    r = coach.guard_check([pace_src], {})
    _check("pace-only 정지", r is not None, r)
    _check("  사유 = 페이스 초과", r is not None and "페이스 초과" in r, r)

    r = coach.guard_check([floor_src], {})
    _check("floor-only 정지", r is not None, r)
    _check("  사유 = 바닥 임박", r is not None and "바닥 임박" in r, r)

    r = coach.guard_check([clear_src], {})
    _check("둘 다 해당 없음 → 통과(None)", r is None, r)

    # ── (b) pace=false: 페이스 건너뛰고 floor만 ──────────────────────────
    print("[pace=false] 페이스 조건 건너뛰고 floor만 검사")
    cfg_nopace = {"claude": {"m": 0.10, "floor": 0.10, "pace": False}}
    r = coach.guard_check([pace_src], cfg_nopace)
    _check("pace 케이스인데 pace=false → 통과", r is None, r)
    r = coach.guard_check([floor_src], cfg_nopace)
    _check("floor 케이스는 pace=false 여도 정지", r is not None and "바닥 임박" in r, r)

    # ── (c) fail-open: 데이터 오류/결측/조회 예외 → 통과 ─────────────────
    print("[fail-open] 데이터 오류/결측/조회 예외 → 통과")
    only5h = _src(_usage(_win(0.50, 0.50, wm=300)))      # 7일 윈도우 아예 없음(5h만)
    _check("7일 윈도우 없음(5h만) → 통과", coach.guard_check([only5h], {}) is None)
    null_up = _src(_usage({"windowMinutes": 10080, "usedPercent": None, "resetsAt": _iso(5000)}))
    _check("usedPercent=null → 통과", coach.guard_check([null_up], {}) is None)
    no_reset = _src(_usage({"windowMinutes": 10080, "usedPercent": 50}))   # resetsAt 누락 + used>0
    _check("resetsAt 누락(used>0) → 통과", coach.guard_check([no_reset], {}) is None)
    _check("조회 예외(bad mock 시나리오) → 통과", coach.guard_check([("claude", "mock", "9")], {}) is None)
    _check("소스 없음 → 통과", coach.guard_check([], {}) is None)

    # ── (d1) load_guard_config: provider별 값 + 기본 폴백 + gemini 별칭 ──
    print("[load_guard_config] provider별 값·기본 폴백·gemini 별칭·fail-open")
    g1 = _cfg_file({"claude": {"m": 0.35, "floor": 0.25, "pace": False},
                    "gemini": {"m": 0.5, "floor": 0.5, "pace": True}})
    cfg = coach.load_guard_config(g1)
    _check("claude 값 그대로 적용", cfg["claude"] == {"m": 0.35, "floor": 0.25, "pace": False}, cfg["claude"])
    _check("codex 누락 → GUARD_DEFAULT", cfg["codex"] == coach.GUARD_DEFAULT, cfg["codex"])
    _check("gemini→antigravity 별칭(m=0.5)", cfg["antigravity"]["m"] == 0.5, cfg["antigravity"])

    cfg2 = coach.load_guard_config(_cfg_file(raw="[]"))          # non-dict JSON
    _check("non-dict JSON([]) → 모두 기본값",
           cfg2["claude"] == coach.GUARD_DEFAULT and cfg2["codex"] == coach.GUARD_DEFAULT)
    cfg3 = coach.load_guard_config(_cfg_file(raw="{잘못된 json"))  # 파싱 불가
    _check("잘못된 JSON → 기본값(fail-open)", cfg3["claude"] == coach.GUARD_DEFAULT)

    # ── (d2) provider별 설정이 판정까지 종단 적용 (같은 usage, 다른 provider) ─
    print("[provider 설정 적용] 같은 usage, provider별 m/floor 가 판정을 갈라야")
    usageX = _usage(_win(0.40, 0.50))   # left40% / timeLeft0.50
    # claude 만 m=0.05(빡빡). codex 는 누락 → 기본 m=0.10.
    cfg4 = coach.load_guard_config(_cfg_file({"claude": {"m": 0.05, "floor": 0.10, "pace": True}}))
    r_claude = coach.guard_check([_src(usageX, "claude")], cfg4)   # m=0.05 → 0.40<0.45 정지
    r_codex  = coach.guard_check([_src(usageX, "codex")],  cfg4)   # 기본 m=0.10 → 0.40<0.40 아님 → 통과
    _check("동일 usage, claude(m=0.05) → 정지", r_claude is not None, r_claude)
    _check("동일 usage, codex(기본 m=0.10) → 통과", r_codex is None, r_codex)

    usageY = _usage(_win(0.08, 0.10))   # left8% / timeLeft0.10
    # claude 만 floor=0.05(낮춤). pace 는 timeLeft-m=0 이라 어차피 안 걸림.
    cfg5 = coach.load_guard_config(_cfg_file({"claude": {"m": 0.10, "floor": 0.05, "pace": True}}))
    r_def = coach.guard_check([_src(usageY, "claude")], {})     # 기본 floor=0.10 → 0.08<0.10 정지
    r_low = coach.guard_check([_src(usageY, "claude")], cfg5)   # floor=0.05 → 0.08<0.05 아님 → 통과
    _check("left8%, 기본 floor=0.10 → 정지", r_def is not None and "바닥" in r_def, r_def)
    _check("동일 usage, floor=0.05 → 통과", r_low is None, r_low)

    # ── (+) guard_enabled: 플래그 파일 존재 = on / 부재 = off ────────────
    print("[guard_enabled] 플래그 파일 존재 여부 = on/off")
    orig = coach.GUARD_ENABLED_PATH
    flag_dir = tempfile.mkdtemp(); _tmpdirs.append(flag_dir)
    flag = os.path.join(flag_dir, "guard-enabled")
    coach.GUARD_ENABLED_PATH = flag
    try:
        _check("플래그 없음 → off(False)", coach.guard_enabled() is False)
        open(flag, "a").close()
        _check("플래그 생성 → on(True)", coach.guard_enabled() is True)
        os.remove(flag)
        _check("플래그 제거 → off(False)", coach.guard_enabled() is False)
    finally:
        coach.GUARD_ENABLED_PATH = orig

    _cleanup()
    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL -> {_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
