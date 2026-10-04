# p100 persistent-chat concierge host adapter

`codex_chat_tick.py` is the host-specific wakeup wrapper for the pinned p100
concierge chat (issue #69). It is **not** part of the portable concierge
(`aflow/concierge.py`) and must not import from or into it.

## Host boundary

- Runs only on host `codex`, from checkout `/root/code/agent_flow`, against the
  pinned thread `01a0f111-fd5f-7273-8bb8-bfaef116bddd` and the local Codex
  app-server Unix socket. `main()` enforces all three; never loosen them.
- The existing cron launcher holds the bootstrap `flock` for the whole
  invocation. This wrapper's job is to stay in the foreground — through
  ownership-visibility waits, reconnects, supervised execution, and cleanup —
  so that parent lock is never released while a possibly owned turn is live.
- One invocation makes at most one `turn/start`. A lost or rejected reply
  never authorizes a second start or a replacement nonce. Only an exact
  `userMessage.clientId` nonce match proves ownership; an accepted turn ID,
  most-recent turn, thread activity, or a completion event alone never does.
- One monotonic 780-second deadline from service start covers everything.
  Never reset it after a reply, reconnect, or nonce match. The final five
  seconds are reserved for the exact-owned status check and, only then, one
  interrupt of the exact proven-owned turn. Unproven turns are never
  interrupted, steered, or replaced.

## Private receipts

Receipts live under `/var/lib/aflowd/concierge/`, are written atomically with
mode `0600`, and are capped at 64 KiB. They contain operational identities,
statuses, and timestamps only. Never persist prompt contents, tokens, server
exception text, events, or model output.

## Tests

`tests/test_concierge_chat_tick.py` is mock-only: real temporary Unix
WebSocket servers with the production RPC code, an injected clock, temporary
state/socket paths, and harmless fake CLI children. Tests must never touch the
real host socket, service, auth, or state, and must stay fast (no real
13-minute waits).

## Installation

Installation to `/usr/local/bin/aflow-concierge-chat-tick` is a
coordinator-only step after reviewed commits reach `origin/main` with
exact-SHA green CI. Workers must not install, restart services, edit cron,
change goals, or create chats.
