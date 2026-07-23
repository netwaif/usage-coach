# codexbar usage JSON — 입력 스키마 (1부 코어용)

실측 확보 2026-06-25 (usage-test pane, codexbar 2.1.191). 샘플 원본 = `usage-samples/{claude,codex,gemini}.json`.

## 조회 명령 (provider별 별도 — `--provider all`은 hang)
```bash
codexbar usage --provider claude  --format json --pretty
codexbar usage --provider codex --source cli --format json --pretty   # codex는 반드시 --source cli 별도
codexbar usage --provider antigravity --format json --pretty
```
- ⚠️ `codexbar --provider all` = **hang 확인됨**(중단 필요). provider별로 따로 받아 합칠 것.
- ⚠️ codex는 `--source cli` 명시(별도 조회). 그래야 RPC→PTY로 정확. (다른 세션 codex 작업서도 동일 권고.)

## 공통 스키마 (provider당 배열 1원소)
```jsonc
{
  "provider": "claude|codex|antigravity",
  "source": "web|codex-cli|api",
  "version": "...",            // antigravity엔 없음
  "usage": {
    "accountEmail": "...",
    "loginMethod": "Claude Pro|plus|Free",
    "primary":   { "usedPercent": N, "windowMinutes": M, "resetsAt": "ISO8601Z", "resetDescription": "..." },
    "secondary": { ... } | null,
    "tertiary":  { ... } | null,
    "extraRateWindows": [ { "id","title","window":{usedPercent,windowMinutes} } ],  // claude만. Max 구독은 "Fable only"(id=claude-weekly-scoped-fable, 7일) 포함 — coach가 표시 전용 라인으로 노출(판정 미반영). 기본 구독엔 없음
    "providerCost": { "limit","used","currencyCode","period" },                      // claude
    "updatedAt": "ISO8601Z"
  },
  "credits": { "remaining": N, "events": [] }   // codex만
}
```
잔량% = `100 - usedPercent`. reset까지 = `resetsAt - now`.

## ★ 핵심 발견 (설계 제약)

**1. 윈도우는 `windowMinutes`로 분류해야 함 (primary/secondary 위치 가정 금지).**
| windowMinutes | 의미 |
|---|---|
| 300 | 5시간 |
| 10080 | 7일 (7×1440) |
| 1440 | 1일 (현행 provider 없음 — 코드 fallback으로만 인식) |

**2. provider마다 윈도우 구성이 다름:**
- **Claude(Pro)**: primary=5h, secondary=7d. → A~D 교차분석 그대로 적용 가능.
- **Codex(plus)**: primary=5h, secondary=7d, +`credits.remaining`. → 동일 적용.
- **Antigravity(별칭 gemini)**: primary/secondary **모두 10080(7일)** 페이스형. 5h(300)·일일(1440) 윈도우 없음 → **소진형이 없는 페이스 전용 provider**. → 5h+7d 교차분석 대신 **7일 페이스 단일 분기**(🔴/🟢/⚪)로 코칭.
  - ⚠️ codexbar가 모델·기능별 주간 하위 한도를 같은 10080으로 여러 개 줄 수 있음 → 가장 빡빡한(남은 % 최소) 1개로 통합해 처리(동일값 중복도 1개로 묶임).

**3. 무효 데이터 가드 필수:**
- `windowMinutes`/`usedPercent`가 null·누락이거나 `resetsAt`가 epoch(`1970-01-01T00:00:00Z`)/비정상이면 그 윈도우는 무효로 스킵. (codexbar가 간헐적으로 null을 주는 경우 방어 → provider 전체 크래시 방지.)

## A~D 권고 로직에의 함의
- 입력 정규화: provider별 JSON → `{provider, windows: [{kind: 5h|7d, leftPercent, minutesToReset}]}` 형태로 windowMinutes 기준 매핑.
- 5h = use-it-or-lose-it(소진), 7d = 페이스 관리. Antigravity는 7d만 → 소진형 없이 페이스 단일 분기.
- reset 임박 동적판정: `minutesToReset` 안에 `leftPercent`를 다 못 쓸 양이면 "소진 권고".
