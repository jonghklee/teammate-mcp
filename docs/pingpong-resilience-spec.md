# Spec: Ping-pong 회복력 (배달된 메일이 굶지 않게)

상태: **구현됨 (라이브 활성화는 사용자 확인 대기).**
- **A. Stop 훅**: `hooks/stop_inbox_drain.py` 신설 + `bin/install-claude` 2.7
  + `templates/settings.claude.json`에 등록. ⚠️ 라이브 `~/.claude/settings.json`엔
  아직 안 넣음 — auto-continue가 진짜 해결인지 미검증 + 기존 notify Stop 훅과
  공존 필요. `install-claude` 실행 시 활성화됨.
- **B. watcher 굶주림 탈출구**: `watcher.py` `_screen_user_is_typing` +
  `_wake_action` + `_scan_once` 통합. **즉시 적용 (watchdog 재시작 시).**
- **C. inject 유실 가드**: `server.py` — unlink를 verify 이후로 이동, 전달
  확인 시에만 삭제. **즉시 적용.**

테스트: `test_watcher.py`(+4), `test_stop_hook.py`(+4), `test_iterm`/기존
`_body_stuck` 커버. 전체 84 passed.

---

(원본 DRAFT 노트 — 설계 근거)

작성 근거: 2026-05-24 진단 세션. 사용자 호소 = "멈춰있다 / 끝나고 보고가
안 온다 / ping pong이 안돼".

---

## 1. 문제 (증거 기반)

배달(mailbox 기록)은 정상인데 **수신자가 배달된 메일을 자동으로 처리/응답하지
않는다.**

증거:
- `claude3/inbox`에 evalworker2의 답장이 `undrained`로 남아 있었음
  (`processed=29`인데 최신 1건이 안 빠짐).
- watchdog 로그 카운트(하루):
  - `skip-busy claude3` = **25035**
  - `woke claude3` = **31**
  - `skip-busy evalworker2` = 866 / `woke` = 3
- 실시간 로그가 2초마다 `skip-busy label=claude3 (1 pending msg)` 반복.

즉 "내 송신이 안 된다"는 **오진**이었고(로그상 송신·배달 정상), 진짜 문제는
**수신측 자동 깨우기(wake) 정책**이다.

## 2. 근본 원인 (코드 위치)

`src/teammate_mcp/watcher.py`
- `_screen_compose_is_empty()` (≈L266): 화면 마지막 25줄에서 `❯` 프롬프트
  라인을 찾아 **그 뒤가 비어 있을 때만** True. 프롬프트가 안 보이면
  (= Claude가 thinking/툴 실행 중) `False`.
- `_scan_once()` (≈L409-418): `if _screen_compose_is_empty(...) → _wake()`,
  `else → log "skip-busy"`. **else 분기에 탈출구가 없다.**

결과: 항상 작업 중인 오케스트레이터 페인(claude3)은 스캔 시점마다 busy로
보여 영원히 skip → 배달된 답장이 starvation. 인터랙티브 Claude 페인의
구조적 한계.

부차 위험:
- `src/teammate_mcp/server.py` inject 경로(≈L643-651, `ask.inbox_unlinked_inline`):
  키스트로크 "성공 가정" 후 mailbox 사본을 unlink → 실제 미전달 시 양쪽 유실.
  (로그상 `inbox_unlinked_inline` 2118회, `recovery_failed_still_stuck` 219회.)

## 3. 제안 해결책 (우선순위)

### A. Stop 훅 기반 inbox 자동 drain — **근본/결정론적**
화면 스크랩 타이밍에 의존하지 않고, **각 Claude 페인이 턴을 끝내는 순간**
(Stop 이벤트) inbox를 확인해 처리 루프를 잇는다.

- `~/.claude/settings.json`(+ `templates/settings.claude.json`)의 `Stop` 훅에
  `teammate-mcp drain` 연동 hook 추가.
- 동작: Stop 시 자기 label의 inbox를 확인 → 메일이 있으면 그 내용을 다음
  처리로 주입(또는 stdout으로 컨텍스트 반환). 메일이 없으면 **아무것도 안 함**
  (무한 루프 방지).
- 멱등성: 처리한 메일은 `processed/`로 이동(`mark_processed`)해 재주입 방지.

### B. watcher 굶주림 탈출구 — **안전망**
`watcher.py` `_scan_once()` 보강:
- label별 "가장 오래된 미깨움 메일의 최초 관측 시각"을 추적.
- `대기시간 > STARVATION_TIMEOUT`(예: 90s)이고 화면이 **"유저 타이핑 중"이
  아니라 "Claude 작업 중"**(= `❯` 라인 자체가 없음, compose에 유저 텍스트 없음)
  이면 **강제 wake** 1회.
- 유저가 반쯤 입력한 compose(`❯ <텍스트>`)는 강제 wake 대상에서 제외 → 입력
  손상 없음.

### C. inject 유실 가드 — **별개 버그**
`server.py` inject 성공 판정을 엄격화하거나, 전달 확인 전에는 mailbox 사본을
unlink하지 않는다(수신자 훅이 중복 dedupe).

## 4. 수용 기준 (검증 가능)

1. **A**: claude3가 작업 중 evalworker2가 답장 → claude3가 현재 턴을 마치면
   별도 유저 입력 없이도 그 답장이 자동으로 처리됨. (재현: worker가 ask로
   답장 → orchestrator의 Stop 직후 inbox `processed`로 이동 + 응답 발생.)
2. **A**: inbox가 비어 있을 때 Stop 훅이 새 턴을 유발하지 않음(무한 루프 X).
3. **B**: 인위적으로 90s+ 묵힌 메일 + "작업 중" 화면 상태에서 watcher가
   `woke`를 1회 기록. 유저 타이핑 중 화면에서는 강제 wake 안 함.
4. **C**: inject 실패(키스트로크 미전달) 케이스에서 mailbox 사본이 보존되어
   수신자 훅이 회수 가능.
5. 회귀: 기존 정상 wake/ drain 경로 동작 유지(`tests/` green).

## 5. 리스크 / 오픈 퀘스천

- Stop 훅 auto-continue가 비용/무한루프를 유발하지 않는지(가드 B 필수).
- 강제 wake가 드물게 유저 입력과 경합할 가능성 → "Claude 작업 중" 판정의
  정확도에 의존. 화면 스냅샷 안정성(연속 N회 동일) 추가 검토.
- **이 가설(A가 진짜 해결인지)** 은 미검증. 구현 후 실측으로 확정한다.
