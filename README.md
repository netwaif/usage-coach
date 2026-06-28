# usage-coach

[codexbar](https://github.com/steipete/CodexBar)의 사용량 데이터를 읽어 **"지금 어떤 크기의 작업을 돌리면 되는지 + 왜 + (필요시) 언제"**를 알려주는 터미널 도구. 단순 정보 표시가 아니라 **행동 권고**가 출력이다.

Claude · Codex · Antigravity의 5시간·7일 한도를 교차 분석해 5가지 상황으로 코칭하고, 에이전트 루프용 **요금 가드**(`--hook` / `--guard-check`)를 함께 제공한다.

## 핵심 개념

- **5시간 한도** = 안 쓰면 reset으로 사라짐 → 남으면 아낌없이 소진(use-it-or-lose-it).
- **7일 한도** = 페이스 관리.
- 코어 비교 = `left`(잔량 비율) vs `timeLeft`(reset까지 시간 비율). 둘 중 빡빡한 쪽이 작업 크기 상한.

## 출력 5종

| | 상황 | 권고 |
|--|--|--|
| 🔴 | 7일 한도가 빠듯 | 큰 작업은 미루기 |
| 🟢 | 5시간 넉넉 + 7일 여유 | 지금 큰 작업 |
| ⏳ | 5시간 부족 + 초기화 임박 | 곧 reset, 잠깐 대기 |
| 🟡 | 5시간 부족 + 페이스 뒤처짐 | 작은 작업만 |
| ⚪ | 평소 | 평소대로 |

## 요구사항

- Python 3 (표준 라이브러리만 — 의존성 없음)
- [codexbar](https://github.com/steipete/CodexBar) — 사용량 데이터 소스(`codexbar` CLI)

## 설치

```bash
git clone https://github.com/netwaif/usage-coach.git
cd usage-coach
# 전역 명령으로 쓰려면 심링크(선택)
ln -s "$PWD/coach.py" /usr/local/bin/coach
```

## 사용법

```bash
coach                                    # 전체 provider 라이브 조회 + watch
coach --providers claude,codex           # 특정 provider만
coach --once                             # 한 번만 찍고 종료
coach --mock all                         # 출력 5종 데모(모킹)
coach --file samples/claude.json:claude  # JSON 파일에서 읽기
coach --json                             # 머신 출력(JSON 1줄) — 에이전트 루프용
```

codexbar 제약: `--provider all`은 hang하므로 provider별로 따로 조회한다(coach가 자동 처리). codex는 `--source cli`.

## 요금 가드 (에이전트 루프)

긴 자율 루프가 사용량을 보고 **위험하면 스스로 멈추게** 한다.

- `coach --hook` — Claude Code Stop 훅용. 7일 가드. 정지 시 `{"continue":false}` 출력, 통과 시 무출력.
- `coach --guard-check` — provider 중립 체크. 정지 시 exit≠0 + 사유, 통과 시 exit 0.
- `coach guard on|off|status` — 런타임 스위치.

판정 = 하이브리드 OR: `left < timeLeft − m`(페이스) **또는** `left < floor`(절대). 데이터 오류 시 fail-open(통과). 설정 = `~/.config/usage-coach/guard.json` (provider별 `m`/`floor`, 기본 0.10/0.10).

## 라이선스

[MIT](./LICENSE)
