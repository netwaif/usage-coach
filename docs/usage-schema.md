# codexbar usage JSON — 입력 스키마 (1부 코어용)

실측 확보 2026-06-25 (usage-test pane, codexbar 2.1.191). 샘플 원본 = `usage-samples/{claude,codex,gemini}.json`.

## 조회 명령 (provider별 별도 — `--provider all`은 hang)
```bash
codexbar usage --provider claude  --format json --pretty
codexbar usage --provider codex --source cli --format json --pretty   # codex는 반드시 --source cli 별도
codexbar usage --provider gemini --source api --format json --pretty
```
- ⚠️ `codexbar --provider all` = **hang 확인됨**(중단 필요). provider별로 따로 받아 합칠 것.
- ⚠️ codex는 `--source cli` 명시(별도 조회). 그래야 RPC→PTY로 정확. (다른 세션 codex 작업서도 동일 권고.)

## 공통 스키마 (provider당 배열 1원소)
```jsonc
{
  "provider": "claude|codex|gemini",
  "source": "web|codex-cli|api",
  "version": "...",            // gemini엔 없음
  "usage": {
    "accountEmail": "...",
    "loginMethod": "Claude Pro|plus|Free",
    "primary":   { "usedPercent": N, "windowMinutes": M, "resetsAt": "ISO8601Z", "resetDescription": "..." },
    "secondary": { ... } | null,
    "tertiary":  { ... } | null,
    "extraRateWindows": [ { "id","title","window":{usedPercent,windowMinutes} } ],  // claude만
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
| 1440 | 1일 (Gemini) |

**2. provider마다 윈도우 구성이 다름:**
- **Claude(Pro)**: primary=5h, secondary=7d. → A~D 교차분석 그대로 적용 가능.
- **Codex(plus)**: primary=5h, secondary=7d, +`credits.remaining`. → 동일 적용.
- **Gemini(Free, 별도 계정)**: primary/secondary/tertiary **모두 1440(일일)**. 5h/7d 개념 없음. → **제외 아님. "daily 윈도우" 트랙으로 별도 취급.** reset 시각은 유효(secondary/tertiary `resetsAt`=익일, "Resets in 23h 59m"). daily는 주기 짧아 **5h와 같은 use-it-or-lose-it(소진) 성격** — reset 임박+잔량多면 "오늘 안에 소진" 권고(A의 daily 버전). 페이스 관리(7d)보다는 소진.
  - ⚠️ title 라벨 없음(claude의 extraRateWindows엔 title 있음). secondary/tertiary 동일 resetsAt → 사실상 같은 일일 reset으로 묶어 처리.

**3. 무효 데이터 가드 필수:**
- Gemini **primary만** `resetsAt: "1970-01-01T00:00:00Z"` + `usedPercent: 100` = placeholder/무효 → 스킵. (secondary/tertiary는 유효하니 그걸 daily 기준으로 사용.) epoch·비정상 reset은 일괄 스킵.

## A~D 권고 로직에의 함의
- 입력 정규화: provider별 JSON → `{provider, windows: [{kind: 5h|7d|daily, leftPercent, minutesToReset}]}` 형태로 windowMinutes 기준 매핑.
- 5h = use-it-or-lose-it(소진), 7d = 페이스 관리. daily(Gemini)는 둘 사이 성격 — 설계 시 판단.
- reset 임박 동적판정: `minutesToReset` 안에 `leftPercent`를 다 못 쓸 양이면 "소진 권고".
