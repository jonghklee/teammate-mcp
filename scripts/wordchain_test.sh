#!/usr/bin/env bash
# wordchain_test.sh — 3-pane 끝말잇기 라운드트립 검증 스크립트.
#
# 검증하는 스킬 (단 하나):
#   • Bash CLI:  `teammate-mcp ask <label> "<question>"`     (default async, mailbox)
#   • 답장 수신:  `~/.teammate-mcp/mailbox/<self>/inbox/*.json` 폴링
#   • Wake:      별도로 `teammate-mcp watch &`가 띄워져 있다고 가정
#                (idle Claude 페인을 "."로 깨워서 hook 발화시킴)
#
# 즉 우리가 쓰는 핵심 스킬은 단 두 가지:
#   1. teammate-mcp ask  — async 메일 발송
#   2. teammate-mcp watch — idle 페인 wake daemon (hook이 receiving 처리)
#
# 슬래시는 안 씁니다. MCP 툴도 안 씁니다. 순수 Bash CLI + watchdog.
#
# 사용법:
#   chmod +x scripts/wordchain_test.sh
#   ./scripts/wordchain_test.sh                    # 기본 6 라운드
#   ./scripts/wordchain_test.sh "사과" 8           # 시작 단어 + 라운드 수
#
# 사전 요건:
#   1. iTerm 페인 a, b, c 가 등록되어 있고 Claude Code 실행 중
#      (없으면: `TEAMMATE_LABEL=<x> tmclaude` 로 각 페인 띄우기)
#   2. watchdog 실행 중: `teammate-mcp watch &`
#   3. 셀프 라벨이 없으면 caller 라벨로 SELF 환경변수 지정 권장
#
set -euo pipefail

BIN="${BIN:-teammate-mcp}"
if ! command -v "$BIN" >/dev/null 2>&1; then
    BIN="/Users/siheom-yong/programming/teammate-mcp/.venv/bin/teammate-mcp"
fi

START_WORD="${1:-사과}"
ROUNDS="${2:-6}"
# Labels can be overridden via env (LABELS="x y z") or extra args
# (./wordchain_test.sh "사과" 4 x y z). Default a/b/c kept for the
# common explicit-TEAMMATE_LABEL setup; for auto-registered panes
# pass the real labels in.
if [[ -n "${LABELS:-}" ]]; then
    read -r -a LABELS <<< "$LABELS"
elif [[ "$#" -ge 3 ]]; then
    LABELS=("$3" "$4" "$5")
else
    LABELS=(a b c)
fi

# 자기 라벨 resolve (답장은 SELF의 inbox로 옴)
SELF="${TEAMMATE_LABEL:-}"
if [[ -z "$SELF" ]]; then
    SELF="$($BIN whoami 2>/dev/null | grep -oE '[a-zA-Z0-9_]+' | head -1 || echo unknown)"
fi
if [[ -z "$SELF" || "$SELF" == "(unregistered)" || "$SELF" == "unknown" ]]; then
    echo "ERROR: 이 페인이 등록되지 않았어. 먼저 'teammate-mcp register' 실행." >&2
    exit 2
fi

echo "═══════════════════════════════════════════════════════════════"
echo " 끝말잇기 e2e 테스트"
echo "   시작 단어: $START_WORD"
echo "   라운드  : $ROUNDS"
echo "   참가자  : ${LABELS[*]}"
echo "   caller  : $SELF"
echo "   skill   : teammate-mcp ask (Bash CLI, async mailbox)"
echo "═══════════════════════════════════════════════════════════════"

# 페인 alive 확인
echo
echo "[준비] alive 검증…"
for L in "${LABELS[@]}"; do
    if ! $BIN exists "$L" >/dev/null 2>&1; then
        echo "  ✗ $L 등록 안 됨 — 페인을 먼저 띄우고 등록해줘" >&2
        exit 2
    fi
    echo "  ✓ $L"
done
if ! pgrep -f "teammate-mcp watch" >/dev/null 2>&1; then
    echo "  ⚠ watchdog 미실행 — 'teammate-mcp watch &' 권장 (idle 페인 자동 wake)"
fi

# inbox cleanup so we read only THIS test's replies
INBOX_SELF="$HOME/.teammate-mcp/mailbox/$SELF/inbox"
mkdir -p "$INBOX_SELF"
rm -f "$INBOX_SELF"/*.json 2>/dev/null || true

# Pause the hook while we run — otherwise caller pane's
# UserPromptSubmit hook drains files we are polling for, and the
# polling loop times out before it ever sees them. Hook re-enables
# automatically on script exit (trap rm).
LOCK="$HOME/.teammate-mcp/hook-pause.lock"
mkdir -p "$(dirname "$LOCK")"
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

# 라운드 진행: word 가 LABELS[i] → LABELS[(i+1)%3] 으로 흐름
word="$START_WORD"
fails=0
for r in $(seq 1 "$ROUNDS"); do
    sender_idx=$(( (r-1) % 3 ))
    receiver_idx=$(( r % 3 ))
    sender="${LABELS[$sender_idx]}"
    receiver="${LABELS[$receiver_idx]}"
    end_char="${word: -1}"

    echo
    echo "─── Round $r ────────────────────────────────────────"
    echo "  $sender → $receiver  word='$word'  (다음 글자: '$end_char')"

    # 보낼 prompt: receiver는 답장만 (단어 하나) async로 SELF에게.
    msg="끝말잇기 라운드 $r. 직전 단어: '$word'. 너는 '$end_char'로 시작하는 한국어 단어 하나만 답해. 단어만, 다른 말 X. 답은 'teammate-mcp ask $SELF \"<단어>\"' 로 보내."

    "$BIN" ask "$receiver" "$msg" >/dev/null

    # SELF 의 inbox에 receiver 가 답장 도착할 때까지 대기 (max 60s)
    deadline=$(( $(date +%s) + 180 ))
    reply=""
    PROCESSED_SELF="$HOME/.teammate-mcp/mailbox/$SELF/processed"
    mkdir -p "$PROCESSED_SELF"
    # caller pane's UserPromptSubmit hook may drain inbox→processed
    # within milliseconds of arrival, so we poll BOTH dirs.
    while [ "$(date +%s)" -lt "$deadline" ]; do
        for dir in "$INBOX_SELF" "$PROCESSED_SELF"; do
            for f in "$dir"/*.json; do
                [ -f "$f" ] || continue
                from=$(python3 -c "import json,sys; print(json.load(open('$f')).get('from_',''))" 2>/dev/null)
                if [ "$from" = "$receiver" ]; then
                    reply=$(python3 -c "import json,sys; print(json.load(open('$f')).get('body','').strip())" 2>/dev/null)
                    rm -f "$f"  # 다음 라운드를 위해 청소
                    break 2
                fi
            done
        done
        [ -n "$reply" ] && break
        sleep 1
    done

    if [ -z "$reply" ]; then
        echo "  ✗ TIMEOUT: $receiver 가 60s 내에 답하지 않음"
        fails=$((fails+1))
        break
    fi

    # 답을 한 줄 단어로 정리
    next_word=$(echo "$reply" | head -1 | tr -d '[:space:][:punct:]' | head -c 60)
    echo "  $receiver 답: '$next_word'"

    # 끝글자 일치 검증 (소프트 — 한국어 자모 정밀 비교는 스킵, 첫 글자만 대조)
    first_char="${next_word:0:1}"
    if [ "$first_char" != "$end_char" ]; then
        echo "  ⚠ 끝말 위반: '$first_char' ≠ '$end_char' (계속 진행)"
    fi
    word="$next_word"
done

echo
echo "═══════════════════════════════════════════════════════════════"
if [ "$fails" -eq 0 ]; then
    echo " ✅ PASS — $ROUNDS 라운드 완주"
    exit 0
else
    echo " ❌ FAIL — $fails 라운드에서 멈춤"
    exit 1
fi
