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

coach 출력을 **Components V2** 메시지(provider별 컨테이너 + 유니코드 게이지 바)로 조립해 디스코드 웹훅 메시지 하나를 **계속 편집**하는 라이브 대시보드. 채널을 열면 항상 최신 상태가 보인다. 전부 네이티브 텍스트라 데스크톱·모바일 어느 폭에서도 선명하다(구버전 PNG 카드는 세로가 길수록 통째로 축소되는 문제가 있어 교체).

- 구성 = provider별 컨테이너(accent 색 = level) 안에 윈도우별 게이지 바 + **코칭 문구(action·reason 전문)** + 리셋 시각(`<t:..:R>` — 보는 사람 로컬 기준 "3시간 후"처럼 자동 표시).
- **봇 세션 섹션**: Claude Code statusline이 `~/.config/usage-coach/sessions/`에 남기는 스냅샷으로 활동 중 세션(디렉토리·모델·컨텍스트 사용률)을 표시. statusline 스크립트에 스냅샷 기록 몇 줄을 추가해야 한다(입력 JSON의 `session_id`·`context_window.used_percentage` 사용, 30분 무활동 시 목록에서 제외).
  - 스냅샷에 **`project_dir`(세션을 띄운 폴더)를 반드시 함께 기록**할 것 — `current_dir`만 적으면 세션이 하위 폴더로 `cd` 한 순간 봇 행 매칭이 끊겨 표시가 멈춘다(실측). 기록 예:
    ```jq
    {cwd: (.workspace.current_dir // .cwd // ""),
     project_dir: (.workspace.project_dir // .workspace.current_dir // .cwd // ""),
     model: (.model.display_name // ""),
     used: (.context_window.used_percentage // null), ts: (now | floor)}
    ```
    매칭은 `project_dir` 우선, 없으면 `cwd` 폴백이라 옛 스냅샷도 그대로 동작한다.
- level이 🟡/🔴로 **나빠지는 순간에만** 새 메시지를 게시해 푸시 알림을 울린다(평소 편집은 무알림).
- 의존성 없음(coach 본체와 동일 — 표준 라이브러리만).
- 함정: 웹훅으로 components를 보낼 때 URL에 `?with_components=true`가 없으면 필드가 통째로 무시된다(`Cannot send an empty message`, 50006). POST·PATCH(편집) 모두 해당.

```bash
python3 discord_dash.py --out c.json        # 컴포넌트 JSON만 저장(웹훅 미사용, 검증용)
python3 discord_dash.py --mock all --out c.json
python3 discord_dash.py                     # 조회→조립→웹훅 업서트(+악화 핑)
```

설정: `~/.config/usage-coach/discord.json`에 `{"webhook_url": "..."}` (또는 env `DISCORD_WEBHOOK_URL`). 같은 파일의 `"coach_args": ["--account", "antigravity:foo@gmail.com"]`로 대시보드가 추적할 계정을 고정할 수 있다(카드에 계정 이메일 표시됨). 여러 계정을 오가는 provider는 `"all_accounts"`(기본 `["antigravity"]`)에 넣으면 codexbar `--all-accounts`로 **등록된 전 계정을 계정별 바로 표시**하고, 코칭·본문·핑은 가장 여유 있는 계정 기준으로 잡는다(끄려면 `"all_accounts": []`). 상태(메시지 ID·직전 level)는 `discord-state.json`에 저장. 주기 실행은 LaunchAgent 등으로 5분 간격 권장(웹훅만 쓰므로 봇 토큰·gateway 불필요).

## 라이선스

[MIT](./LICENSE)
