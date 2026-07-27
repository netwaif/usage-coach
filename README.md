# usage-coach

[codexbar](https://github.com/steipete/CodexBar)의 사용량 데이터를 읽어 **"지금 어떤 크기의 작업을 돌리면 되는지 + 왜 + (필요시) 언제"**를 알려주는 터미널 도구. 단순 정보 표시가 아니라 **행동 권고**가 출력이다.

Claude · Codex · Antigravity의 5시간·7일 한도를 교차 분석해 5가지 상황으로 코칭하고, 에이전트 루프용 **요금 가드**(`--hook` / `--guard-check`)를 함께 제공한다.

## 핵심 개념

- **5시간 한도** = 안 쓰면 reset으로 사라짐 → 남으면 아낌없이 소진(use-it-or-lose-it).
- **7일 한도** = 페이스 관리.
- **Fable 5 주간 한도**(Claude Max 구독) = 표시 전용 `Fable` 라인. 코칭·요금가드 판정에는 미반영이며, 기본 구독처럼 데이터가 없으면 라인 자체를 생략한다. `--json`에는 `fable_7d` 키로 나온다.
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
coach --account antigravity:foo@gmail.com  # 계정 지정(codexbar --account 패스스루, 반복 가능)
coach --json                             # 머신 출력(JSON 1줄) — 에이전트 루프용
```

codexbar 제약: `--provider all`은 hang하므로 provider별로 따로 조회한다(coach가 자동 처리). codex는 `--source cli`.

## 요금 가드 (에이전트 루프)

긴 자율 루프가 사용량을 보고 **위험하면 스스로 멈추게** 한다.

- `coach --hook` — Claude Code Stop 훅용. 7일 가드. 정지 시 `{"continue":false}` 출력, 통과 시 무출력.
- `coach --guard-check` — provider 중립 체크. 정지 시 exit≠0 + 사유, 통과 시 exit 0.
- `coach guard on|off|status` — 런타임 스위치.

판정 = 하이브리드 OR: `left < timeLeft − m`(페이스) **또는** `left < floor`(절대). 데이터 오류 시 fail-open(통과). 설정 = `~/.config/usage-coach/guard.json` (provider별 `m`/`floor`, 기본 0.10/0.10).

## 디스코드 대시보드 (`discord_dash.py`)

coach 출력을 PNG 게이지 카드로 렌더해 디스코드 웹훅 메시지 하나를 **계속 편집**하는 라이브 대시보드. 채널을 열면 항상 최신 카드가 보인다.

- 카드 구성 = provider별 도넛 게이지(주 윈도우) + 보조 윈도우 바 + **코칭 문구(action·reason 전문)** + 리셋 카운트다운.
- level이 🟡/🔴로 **나빠지는 순간에만** 새 메시지를 게시해 푸시 알림을 울린다(평소 편집은 무알림).
- 이 스크립트만 [Pillow](https://python-pillow.org) 필요(coach 본체는 여전히 무의존성). 한글 폰트는 macOS 내장 AppleSDGothicNeo 사용.

```bash
python3 discord_dash.py --out card.png      # 렌더만(웹훅 미사용, 검증용)
python3 discord_dash.py --mock all --out c.png
python3 discord_dash.py                     # 조회→렌더→웹훅 업서트(+악화 핑)
```

설정: `~/.config/usage-coach/discord.json`에 `{"webhook_url": "..."}` (또는 env `DISCORD_WEBHOOK_URL`). 같은 파일의 `"coach_args": ["--account", "antigravity:foo@gmail.com"]`로 대시보드가 추적할 계정을 고정할 수 있다(카드에 계정 이메일 표시됨). 여러 계정을 오가는 provider는 `"all_accounts"`(기본 `["antigravity"]`)에 넣으면 codexbar `--all-accounts`로 **등록된 전 계정을 계정별 바로 표시**하고, 코칭·본문·핑은 가장 여유 있는 계정 기준으로 잡는다(끄려면 `"all_accounts": []`). 상태(메시지 ID·직전 level)는 `discord-state.json`에 저장. 주기 실행은 LaunchAgent 등으로 5분 간격 권장(웹훅만 쓰므로 봇 토큰·gateway 불필요).

## 라이선스

[MIT](./LICENSE)
