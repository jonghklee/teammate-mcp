# 끝말잇기 e2e 테스트 — 100% 재현 절차

이 문서는 v0.9.1의 통신 fix를 사용자가 직접 시현하는 절차입니다.
저(Claude)도 정확히 이 절차로 테스트했습니다.

## 무엇을 검증하는가

3개의 Claude Code 인스턴스(a, b, c)가 끝말잇기를 자동으로 4 라운드 진행:
- caller(이 문서를 따라 하는 본인 페인)가 a → b → c → a 순환으로 ASK를 송신
- 각 receiver는 사용자 입력 없이 **watchdog가 자동으로 깨움**
- receiver의 hook이 inbox 자동 drain → Claude가 한국어 단어 답장
- 답장은 caller의 inbox 파일로 도착 → 스크립트가 다음 라운드의 단어로 사용

성공 시 4 라운드 완주 메시지 + exit 0.

## 사전 요건 (한 번만 설정)

```bash
# 1) repo clone + install
cd ~/programming
git clone https://github.com/jonghklee/teammate-mcp.git || cd teammate-mcp && git pull
cd teammate-mcp
uv venv
uv pip install -e .

# 2) Claude Code 통합 설치 (commands, CLAUDE.md, hook, PATH)
./bin/install-claude

# 3) 새 셸 열어서 PATH 적용 확인
exec zsh
which teammate-mcp   # → <repo>/.venv/bin/teammate-mcp 가 나와야 함
```

## 페인 띄우기 (테스트마다)

iTerm에서 새 창 하나 열고:

```
1) 새 창 (cmd+N)
2) 수직 분할 (cmd+D) → 좌/우 두 개
3) 우측 페인에서 수평 분할 (shift+cmd+D) → 우상/우하 → 총 3 페인

   ┌────────────┬────────────┐
   │            │   pane 2   │
   │  pane 1    ├────────────┤
   │            │   pane 3   │
   └────────────┴────────────┘
```

각 페인에서 다음을 차례로 입력:

```bash
# pane 1
TEAMMATE_LABEL=a tmclaude

# pane 2
TEAMMATE_LABEL=b tmclaude

# pane 3
TEAMMATE_LABEL=c tmclaude
```

각 페인에서 Claude Code가 시작되며 "Trust this folder?" 프롬프트가 뜨면
**Enter (1번 선택지 "Yes")**.

라벨이 status bar에 `[a]`, `[b]`, `[c]` 로 표시되는지 확인.

## watchdog 띄우기 (테스트마다, 한 번)

이 책임을 가질 페인을 한 곳 정함 — 본인이 명령어 입력하는 페인이 적당.
별도 새 페인을 열어 거기서 띄워도 됨.

```bash
# 등록 (없으면 자동 라벨 부여)
teammate-mcp register

# watchdog 백그라운드 실행
teammate-mcp watch &
```

확인:

```bash
pgrep -fa "teammate-mcp watch"
# → 한 줄 떠야 함

teammate-mcp list | grep -E '^a |^b |^c '
# → 세 줄 다 보여야 함
```

## 끝말잇기 실행

```bash
cd ~/programming/teammate-mcp
./scripts/wordchain_test.sh "사과" 4
```

기대 출력:

```
═══════════════════════════════════════════════════════════════
 끝말잇기 e2e 테스트
   시작 단어: 사과
   라운드  : 4
   참가자  : a b c
   caller  : <당신의 라벨>
   skill   : teammate-mcp ask (Bash CLI, async mailbox)
═══════════════════════════════════════════════════════════════

[준비] alive 검증…
  ✓ a
  ✓ b
  ✓ c

─── Round 1 ────────────────────────────────────────
  a → b  word='사과'  (다음 글자: '과')
  b 답: '과수원'

─── Round 2 ────────────────────────────────────────
  b → c  word='과수원'  (다음 글자: '원')
  c 답: '원숭이'

─── Round 3 ────────────────────────────────────────
  c → a  word='원숭이'  (다음 글자: '이')
  a 답: '이불'

─── Round 4 ────────────────────────────────────────
  a → b  word='이불'  (다음 글자: '불')
  b 답: '불고기'

═══════════════════════════════════════════════════════════════
 ✅ PASS — 4 라운드 완주
```

각 라운드는 30초~3분 걸림 (Opus extended thinking 시간 + watchdog wake 사이클).

## 직접 눈으로 확인할 수 있는 흔적

### 1) caller 본인 inbox에 답장 파일이 쌓임
```bash
ls ~/.teammate-mcp/mailbox/<당신라벨>/processed/ 2>&1 | tail
```
→ Round 1~4 시점에 from=b, c, a 답장 JSON.

### 2) 각 페인의 Claude Code 화면에 ASK + Bash 호출 흔적
```
[teammate-mcp inbox: ASK from=<당신> job_id=...]
끝말잇기 라운드 1. 직전 단어: '사과'. ...

⏺ Bash(teammate-mcp ask <당신> "과수원" --no-wait)
⏺ <당신> 끝말잇기 답: "과수원" 전송 완료.
```

### 3) watchdog log에 wake 흔적
```bash
tail -20 ~/.teammate-mcp/logs/watchdog.log
```
→ `woke label=a for N pending msg` 류 줄들.

### 4) hook log에 drain 흔적
```bash
tail -20 ~/.teammate-mcp/logs/hook-drain.log
```
→ `drained N for label=a` 류 줄들.

## 정리

테스트 끝나면:
```bash
# 페인 닫음 (cmd+W로 각각, 또는 창 닫음)
# watchdog 종료
pkill -f "teammate-mcp watch"

# stale 항목 자동 정리 (다음 list/register 시 자동 호출되지만 즉시도 가능)
teammate-mcp prune
```

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `✗ TIMEOUT: <X>가 60s 내 답하지 않음` | watchdog 미실행 → `teammate-mcp watch &` |
| `✗ <X> 등록 안 됨` | 그 페인에서 `tmclaude` 다시 실행 |
| 한 페인이 응답을 안 보냄 | 해당 페인 화면에서 사용자가 직접 prompt 한 번 보내거나, 페인 재시작 |
| watchdog가 자기 페인을 깨움 (사용자 입력에 "."가 끼어듦) | v0.9.0+ 부터 자기 페인 skip 로직 들어감. 버전 확인 `teammate-mcp version` |

## 저(Claude)가 사용한 정확한 절차 (간략화)

1. **페인 자동 spawn**: iterm2 Python API로 새 창에 a/b/c 페인 split + 각각 `TEAMMATE_LABEL=X tmclaude` inject
2. **trust prompt 자동 dismiss**: 각 페인에 Enter inject
3. **watchdog 백그라운드 실행**: `teammate-mcp watch &`
4. **wordchain_test.sh 실행**: 위와 같은 스크립트
5. **결과 확인**: stdout + mailbox 파일 + 페인 화면 캡처 (iterm2 Python API)

자동화 스크립트는 `scripts/wordchain_test.sh` 단 하나. 사람이 GUI 클릭으로 똑같이 따라 할 수 있는 절차이며, 위 GUI 단계가 그 매뉴얼 버전입니다.
