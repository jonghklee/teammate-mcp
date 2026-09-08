# Reliable conversations between Claude panes and Codex threads

User requirement (2026-09-08): agents must actually receive one another's
questions and replies without a human repeatedly checking the other screen.

## Contract

- An address belongs to one pane or one Codex thread. Never guess a pane.
- Persist messages before attempting delivery. Acceptance into the mailbox,
  delivery into a model turn, and an actual reply are different states.
- A reply carries its original question ID. A receipt alone is not a reply.
- Existing completed answers survive duplicate or weaker acknowledgements.
- Codex automatic delivery uses its local app-server protocol. Default policy:
  wait while active, deliver when idle. Never interrupt an existing turn.
- Delivery errors remain inspectable; an ambiguous network result must not
  cause blind resending. No infinite autonomous question/ack loops.
- Old MCP processes retain old code until reconnect. A fresh MCP stdio client
  can exercise updated tools without restarting other people's processes.

## Implementation and adversarial verification cycles

1. **Endpoint identity** — `registry.py`, `server.py`,
   `tests/test_mailbox_endpoint.py`: mailbox registration, collision rejection,
   sender identification, no iTerm access, no stale owner's inbox reuse.
2. **Durability** — `server.py`, `tests/test_processed_receipts.py`: persist a
   reply after keystroke delivery removed inbox; serialize receipt updates;
   preserve completed replies; fail visibly on storage errors.
3. **Automatic delivery** — `codex_transport.py`, `mailbox_worker.py`: probe the
   exact thread, verify direct input capability, queue while active, deliver
   when idle, retain notification state across worker restarts. Unit tests use
   fake app-server replies; live verification uses this user's registered thread.
4. **Conversation completion** — correlate a reply with its source question,
   inspect pending/delivered/replied state, test duplicate and failed delivery,
   and repeat a live question → answer → acknowledgement with claude39.

## Observed evidence

- Direct MCP inbox exchange succeeded for address `codex-01a07f4a` and token
  `C39-MB-7Q2K`. Claude39 originated the question, Codex answered, and Claude39
  sent an acknowledgement into the same inbox.
- The corresponding live Codex thread reports `canAcceptDirectInput=true`.
- Automatic active-turn delivery succeeded with peer-generated token
  `C39-AW-3M9X`: the question and its acknowledgement arrived as actual new
  inputs in the Codex conversation, without an inbox or screen read.
- Default policy was restored to `idle` after that diagnostic. Live idle-turn
  delivery then succeeded with peer token `C39-ID-5R8V`: question
  `1788844139925-e8e3f8` stayed `waiting` while Codex was active, then the worker
  started turn `01a07f6c-097e-7b43-9a75-f235a4dc26ed` after the previous turn
  ended. The new turn received the question as actual input and sent correlated
  reply `1788844201186-874422`. No extra acknowledgement was requested.
- Correlated live reply `1788843962459-65801d` retained question/conversation ID
  `1788843953327-303fd8`. While the receiver was active, delivery status stayed
  `waiting`; the answer was explicitly inspected and processed in that turn.
- Unit verification: `python -m pytest tests --ignore=tests/test_e2e.py -q`
  passed 124 tests. Live iTerm suite was excluded; real communication checks
  above used only the user-authorized claude39 session.
- Local app-server handshake works using WebSocket over its Unix socket with
  compression disabled. JSONL is not this socket's transport.
- Source: https://learn.chatgpt.com/docs/app-server and local 0.153.4 binary.

## Completion boundary

The initial mailbox exchange does not prove automatic wake, reconnection,
failure recovery, or every existing pane's configuration. Report these
separately and do not mark the whole system complete on a token exchange alone.
