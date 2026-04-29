# teammate-mcp — 마이그레이션 가이드 (v0.5 ~ v0.10.7)

옛 버전 쓰던 분들을 위해 **무엇이 바뀌었고, 어떻게 명령 내려야 하는지** 정리.

## TL;DR — 지금 쓸 수 있는 명령들

```bash
# 등록 (한 번만, 페인 시작 시)
teammate-mcp register-pane                    # alias: register, reg
TEAMMATE_LABEL=worker teammate-mcp register-pane  # 명시적 라벨

# 메시지 보내기 (default async, file-only — 사용자 텍스트 보존)
teammate-mcp ask <label> "<question>"         # 가장 흔함
teammate-mcp ask --wait <label> "<question>"  # 동기 (답장까지 대기)

# 슬래시 (Claude Code 안에서)
/ask <label> <question>                       # Bash CLI 호출 → 빠름

# 자연어 (CLAUDE.md 가이드 따라)
"claude29에게 X 물어봐"                          # → /ask 와 동일
"등록"                                          # → register-pane 즉시

# 받은 메시지 보기 (보통 hook이 자동 drain)
teammate-mcp inbox                            # 본인 inbox
teammate-mcp drain                            # 즉시 drain + 출력

# 관리
teammate-mcp list                             # alive 페인 목록
teammate-mcp prune                            # dead 항목 정리
teammate-mcp whoami                           # 이 페인의 라벨
```

## 핵심 변경사항 (v0.6 → v0.10.7)

### 1. Default async (v0.8.0~)
- 옛: `ask`가 **sync** 기본 (caller가 답장까지 block)
- 새: `ask`가 **async** 기본 (즉시 반환, 답장은 receiver가 reverse async ask로 회신)
- sync 필요시 명시적 `--wait` 또는 `wait=True`

### 2. Compose 보존 (v0.10.0~)
- 옛: receiver 페인에 사용자가 typing 중인 텍스트가 ASK 본문과 **합쳐져 submit** 됨 (compose-merge bug)
- 새: 보내기 전 사용자 텍스트 snapshot → DEL × N으로 clear → ASK inject → 1초 후 사용자 텍스트 type-back

### 3. 동시성 안전 (v0.10.1~)
- 옛: 두 sender가 같은 receiver에 동시 ASK → race condition으로 사용자 텍스트 손실
- 새: per-target `flock` 직렬화 — OS 레벨이라 다른 프로세스끼리도 안전

### 4. 라벨 자동 정리 (v0.8.3~)
- 옛: 페인 닫혀도 registry에 stale 항목으로 남음 → claude5 죽으면 다음 페인이 claude6 받음
- 새: `prune_dead`가 list/register 시 자동 호출 → 라벨 번호 재활용

### 5. Mailbox 라벨 충돌 방지 (v0.9.2~)
- 옛: claude5 페인 닫고 새 페인 → 옛 mailbox/claude5/ 그대로 → 새 페인의 hook이 stale 메시지 drain
- 새: register 시 그 라벨의 옛 mailbox 자동 archive

### 6. 슬래시 커맨드 정비
- 옛: `/team-ask` (느림, MCP 경유)
- 새: `/ask` (빠름, Bash CLI 직접) — 권장
- 새: `/register` `/reg` (alias) — register-pane 단축
- 새: `/drain` — 인박스 수동 drain

### 7. 등록 키워드 (v0.8.2~)
사용자가 단일 단어 "등록", "register" 같은 거 입력하면 **즉시 register-pane 실행** (LLM이 묻지 않음). 빠르게 페인 등록.

## 옛 버전을 쓰던 분들의 교차점

### 옛 `wait=True` (sync) 기본 가정 코드
```python
# 옛 (v0.7 이하)
result = mcp__teammate__ask(target="claude29", question="...")
# result는 답장 텍스트
```
```python
# 새 (v0.8.0+)
result = mcp__teammate__ask(target="claude29", question="...", wait=True)
# 또는 default async로:
result = mcp__teammate__ask(target="claude29", question="...")
# result는 "queued: ..." (답장은 caller의 inbox로 별도 도착)
```

### 옛 keystroke가 사용자 텍스트와 합쳐져 submit
- 옛 동작: receiver 페인에 사용자가 입력 중인 텍스트가 있으면 **그 텍스트와 ASK 본문이 합쳐져** 같이 submit
- 새 동작: 자동 snapshot/restore. 사용자 텍스트는 절대 삭제/소실되지 않음.

### 옛 ESC ESC + Ctrl+U 클리어 시퀀스
- 옛: 일부 환경에서 작동 안 함 → merge 버그
- 새: per-character DEL × len(saved_text) + 4 pad. Claude Code TUI에서 100% 작동.

### 옛 codex 호환을 위한 추가물 (v0.10.3~v0.10.6에서 잠시)
- 옛 (v0.10.3~v0.10.6): codex 호환을 위해 ESC ESC + Ctrl+U + Ctrl+A + Ctrl+K + DEL × N 멀티-키 클리어. compose prompt regex에 `>` `▌` 추가.
- 문제: `>`가 ASK 본문 내부 `<your reply>`를 잘못 매칭 → false-positive snapshot, 이상한 텍스트로 restore. 멀티-키도 일부 환경에서 enter 안 눌러지는 부작용.
- **새 (v0.10.7)**: 모두 제거. 단순 DEL × N + ❯ 만 매칭. Claude Code 페인끼리 통신 안정성 회복.
- Codex 페인은 hook 시스템 없으므로 file-only 메시지 받으려면 `teammate-mcp drain` 수동 호출 필요.

## 옛 버전 쓰던 분들이 새 버전으로 전환하는 법

```bash
# 1. 업데이트
cd ~/programming/teammate-mcp
git pull
uv pip install -e .

# 2. 옛 mcp serve 프로세스 모두 죽임
ps aux | grep "teammate-mcp serve" | grep -v grep | awk '{print $2}' | xargs kill

# 3. 새 hook + 슬래시 + CLAUDE.md 동기화
./bin/install-claude

# 4. 새 Claude Code 세션 띄움 (옛 세션은 hook + CLAUDE.md가 메모리에 옛 거 남아있음)
exec zsh
```

새 페인부터 다음 동작 보장:
- compose 보존 (사용자 텍스트 안 사라짐)
- 동시 sender 안전
- 라벨 자동 재활용
- mailbox 충돌 방지

## 옛 명령어 → 새 명령어 매핑

| 옛 (v0.6 이하) | 새 (v0.10.7) | 비고 |
|---|---|---|
| `ask(target, q)` (sync) | `ask(target, q, wait=True)` | default가 async로 바뀜 |
| `register_self()` | `teammate-mcp register-pane` (Bash CLI) | LLM 우회, 빠름 |
| `/team-ask <l> <q>` | `/ask <l> <q>` | Bash CLI 호출, 10× 빠름 |
| (없음) | `/drain` | 수동 inbox drain |
| (없음) | `teammate-mcp prune` | dead 항목 정리 |
| (없음) | `teammate-mcp inbox` | 본인 inbox 조회 |

## 핵심 보장

> 사용자가 receiver 페인에 typing 중이어도 그 텍스트는 **절대 잃지 않음**.
> 두 sender가 동시에 보내도 race 없이 직렬화됨.
> 옛 페인 닫고 새 페인 열어도 stale mailbox로부터 메시지 누수 없음.

## 자세한 정보

- 재현 가이드: `scripts/REPRODUCE.md`
- 끝말잇기 e2e 테스트: `scripts/wordchain_test.sh`
- README: `README.md`
- 변경 이력: GitHub Releases (v0.5.0 ~ v0.10.7)

GitHub: https://github.com/jonghklee/teammate-mcp
