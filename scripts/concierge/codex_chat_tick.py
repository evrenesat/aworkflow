#!/usr/bin/env python3
"""Wake the pinned concierge chat through its existing writer; cron owns cadence.

Host-specific persistent-chat adapter for p100 (see scripts/concierge/AGENTS.md).
Repairs issue https://github.com/evrenesat/aworkflow/issues/69: an asynchronously
published ownership nonce must not release the foreground lock, and one
invocation supervises the exact proven turn to terminal status or the original
service-start deadline without ever starting twice or interrupting a turn whose
ownership is unproven.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import websockets
import websockets.exceptions

THREAD = "01a0f111-fd5f-7273-8bb8-bfaef116bddd"
ROOT = Path("/root/code/agent_flow")
STATE = Path("/var/lib/aflowd/concierge")
SOCKET = Path("/root/.codex/app-server-control/app-server-control.sock")
CLI_FALLBACK = "/usr/local/bin/codex"

TICK_DEADLINE_SECONDS = 780.0
START_ADMISSION_CUTOFF_SECONDS = 600.0
CLEANUP_WINDOW_SECONDS = 5.0
ADVISORY_SECONDS = 720.0
VISIBILITY_FAST_SECONDS = 30.0
FAST_RETRY_SECONDS = 1.0
SLOW_RETRY_SECONDS = 5.0
RECONNECT_MIN_INTERVAL_SECONDS = 5.0
PAGE_CAP = 10
TURN_PAGE_LIMIT = 5
ITEM_PAGE_LIMIT = 16
TERMINAL_STATUSES = ("completed", "failed", "interrupted")

CLIENT_INFO = {
    "name": "aflow_concierge_cron",
    "title": "p100 AFlow concierge",
    "version": "1.1",
}


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


async def _recv(ws, timeout: float):
    return await asyncio.wait_for(ws.recv(), timeout)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class TickDeadlineExceeded(RuntimeError):
    """The single service-start budget for this invocation is exhausted."""


class TickDeadline:
    """One monotonic deadline, computed once from service start."""

    def __init__(self, started: float, total: float = TICK_DEADLINE_SECONDS) -> None:
        self.started = started
        self.deadline = started + total

    def remaining(self) -> float:
        return max(0.0, self.deadline - _now())

    def exhausted(self) -> bool:
        return self.deadline - _now() <= 0

    def require(self) -> float:
        remaining = self.deadline - _now()
        if remaining <= 0:
            raise TickDeadlineExceeded("tick deadline exhausted")
        return remaining


def store(value: dict, filename: str = "scheduler-wakeup-latest.json") -> None:
    """Atomic private bounded receipt; operational identities only."""
    data = json.dumps(value, indent=2).encode()
    if len(data) > 65536:
        raise ValueError("scheduler receipt exceeds bound")
    fd, name = tempfile.mkstemp(dir=STATE, prefix=".chat-wakeup-")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(name, STATE / filename)


class RPC:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.sequence = 0
        self.events: deque = deque()
        self.late_results: dict = {}

    def next_id(self) -> int:
        self.sequence += 1
        return self.sequence

    async def call(self, method: str, params: dict, deadline: TickDeadline,
                   request_id: int | None = None):
        ident = request_id if request_id is not None else self.next_id()
        remaining = deadline.require()
        await asyncio.wait_for(
            self.ws.send(json.dumps({"id": ident, "method": method, "params": params})),
            remaining,
        )
        while True:
            remaining = deadline.require()
            msg = json.loads(await _recv(self.ws, remaining))
            if msg.get("id") == ident:
                if "error" in msg:
                    raise RuntimeError(f"{method} rejected (code {msg['error'].get('code')})")
                return msg["result"]
            if "method" in msg:
                # Preserve queued events while RPC responses are read.
                self.events.append(msg)
            else:
                # A late reply to an earlier (possibly lost) request id.
                self.late_results[msg["id"]] = msg


async def open_connection(deadline: TickDeadline):
    deadline.require()
    ws = await asyncio.wait_for(
        websockets.unix_connect(str(SOCKET.resolve()),
                                open_timeout=min(10.0, deadline.require()),
                                max_size=4 * 1024 * 4096),
        deadline.require(),
    )
    rpc = RPC(ws)
    try:
        await rpc.call("initialize", {"clientInfo": CLIENT_INFO,
                                      "capabilities": {"experimentalApi": True}}, deadline)
        remaining = deadline.require()
        await asyncio.wait_for(ws.send(json.dumps({"method": "initialized"})), remaining)
    except BaseException:
        ws.transport.abort()
        raise
    return ws, rpc


async def abandon_connection(ws, deadline: TickDeadline) -> None:
    try:
        remaining = deadline.remaining()
        if remaining > 0:
            await asyncio.wait_for(ws.close(), min(1.0, remaining))
    except Exception:
        pass
    finally:
        ws.transport.abort()


async def reconnect(old_ws, deadline: TickDeadline, pacing: dict):
    """Reconnect read-only within the same deadline; never more often than 5s.

    Pacing is measured between consecutive connection attempts (successful or
    failed), not only between failed opens, so a peer that accepts and then
    immediately closes cannot trigger sub-second reconnect storms.
    """
    await abandon_connection(old_ws, deadline)
    while True:
        deadline.require()
        elapsed = _now() - pacing.get("last_attempt", 0.0)
        if elapsed < RECONNECT_MIN_INTERVAL_SECONDS:
            await _sleep(min(RECONNECT_MIN_INTERVAL_SECONDS - elapsed,
                             deadline.require()))
        pacing["last_attempt"] = _now()
        try:
            return await open_connection(deadline)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException,
                RuntimeError):
            if deadline.exhausted():
                raise TickDeadlineExceeded("tick deadline exhausted")


def _is_rpc_rejection(exc: RuntimeError) -> bool:
    """True only for a JSON-RPC error reply from RPC.call, never for invariant failures."""
    return " rejected (code" in str(exc)


def _new_nonce() -> str:
    return str(uuid.uuid4())


def _is_nonce_user_message(entry: dict, nonce: str) -> bool:
    item = entry.get("item", {})
    return item.get("type") == "userMessage" and item.get("clientId") == nonce


async def _turn_contains_nonce(rpc: RPC, deadline: TickDeadline, turn_id: str, nonce: str) -> bool:
    """Check one turn's items directly; opaque cursors, finite per-pass cap."""
    cursor = None
    for _ in range(PAGE_CAP):
        params = {"threadId": THREAD, "turnId": turn_id,
                  "limit": ITEM_PAGE_LIMIT, "sortDirection": "asc"}
        if cursor:
            params["cursor"] = cursor
        page = await rpc.call("thread/items/list", params, deadline)
        for entry in page.get("data", []):
            if _is_nonce_user_message(entry, nonce):
                return True
        cursor = page.get("next_cursor")
        if not cursor:
            # A fully enumerated scan without the nonce is not proof of absence
            # while visibility may still be delayed; the caller keeps supervising.
            return False
    # Page-cap exhaustion is uncertainty, never absence.
    return False


async def _find_nonce_turn(rpc: RPC, deadline: TickDeadline, nonce: str,
                           skip_turn_id: str | None = None) -> dict | None:
    """Recent-turn enumeration used only to discover a lost reply's nonce match."""
    cursor = None
    for _ in range(PAGE_CAP):
        params = {"threadId": THREAD, "limit": TURN_PAGE_LIMIT,
                  "sortDirection": "desc", "itemsView": "notLoaded"}
        if cursor:
            params["cursor"] = cursor
        page = await rpc.call("thread/turns/list", params, deadline)
        for turn in page.get("data", []):
            if skip_turn_id is not None and turn.get("id") == skip_turn_id:
                continue
            if await _turn_contains_nonce(rpc, deadline, turn["id"], nonce):
                return turn
        cursor = page.get("next_cursor")
        if not cursor:
            return None
    return None


async def reconcile_ownership(rpc: RPC, deadline: TickDeadline, nonce: str,
                              accepted_turn_id: str | None):
    """Only an exact nonce match proves ownership.

    Returns ("owned", turn_id) | ("conflict", None) | ("unresolved", None).
    """
    if accepted_turn_id:
        if await _turn_contains_nonce(rpc, deadline, accepted_turn_id, nonce):
            return "owned", accepted_turn_id
        other = await _find_nonce_turn(rpc, deadline, nonce, skip_turn_id=accepted_turn_id)
        if other is not None:
            return "conflict", None
        return "unresolved", None
    turn = await _find_nonce_turn(rpc, deadline, nonce)
    if turn is not None:
        return "owned", turn["id"]
    return "unresolved", None


async def _owned_turn_status(rpc: RPC, deadline: TickDeadline, turn_id: str) -> str | None:
    page = await rpc.call("thread/turns/list",
                          {"threadId": THREAD, "limit": TURN_PAGE_LIMIT,
                           "sortDirection": "desc", "itemsView": "notLoaded"}, deadline)
    for turn in page.get("data", []):
        if turn.get("id") == turn_id:
            return turn.get("status")
    return None


def _drain_terminal_event(rpc: RPC, owned_turn_id: str | None) -> str | None:
    """Apply buffered terminal events only for the exact proven-owned turn."""
    if owned_turn_id is None:
        return None
    for index, event in enumerate(list(rpc.events)):
        if event.get("method") != "turn/completed":
            continue
        params = event.get("params", {})
        if params.get("threadId") != THREAD:
            continue
        turn = params.get("turn", {})
        if turn.get("id") == owned_turn_id and turn.get("status") in TERMINAL_STATUSES:
            del rpc.events[index]
            return turn["status"]
    return None


def _finish(receipt: dict, status: str) -> int:
    receipt.update(status=status, recorded_at=utc(), completed_at=utc())
    store(receipt)
    return 0 if status == "completed" else 1


def _retry_interval(deadline: TickDeadline) -> float:
    elapsed = _now() - deadline.started
    interval = FAST_RETRY_SECONDS if elapsed < VISIBILITY_FAST_SECONDS else SLOW_RETRY_SECONDS
    return min(interval, max(0.0, deadline.deadline - _now()))


async def _await_owned_terminal(ws, rpc: RPC, deadline: TickDeadline,
                                turn_id: str, pacing: dict) -> str | None:
    """Keep foreground supervision until this exact turn stops or time expires."""
    original_ws = ws
    try:
        while not deadline.exhausted():
            terminal = _drain_terminal_event(rpc, turn_id)
            if terminal is not None:
                return terminal
            try:
                status = await _owned_turn_status(rpc, deadline, turn_id)
                if status in TERMINAL_STATUSES:
                    return status
            except TickDeadlineExceeded:
                break
            except (OSError, asyncio.TimeoutError,
                    websockets.exceptions.WebSocketException):
                try:
                    ws, rpc = await reconnect(ws, deadline, pacing)
                except (TickDeadlineExceeded, OSError, asyncio.TimeoutError,
                        websockets.exceptions.WebSocketException):
                    if deadline.exhausted():
                        break
            except RuntimeError:
                # A rejected status read isn't evidence that the owned turn stopped.
                pass
            await _sleep(min(FAST_RETRY_SECONDS, deadline.remaining()))
        return _drain_terminal_event(rpc, turn_id)
    finally:
        if ws is not original_ws:
            await abandon_connection(ws, deadline)


async def _interrupt_and_wait(ws, rpc: RPC, deadline: TickDeadline,
                              receipt: dict, turn_id: str,
                              pacing: dict) -> str | None:
    """Request interruption once; an acknowledgment doesn't prove termination."""
    request_state = "not_attempted"
    if not deadline.exhausted():
        request_state = "uncertain"
        try:
            await rpc.call("turn/interrupt", {"threadId": THREAD, "turnId": turn_id},
                           deadline)
            request_state = "acknowledged"
        except TickDeadlineExceeded:
            pass
        except RuntimeError:
            request_state = "rejected"
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
            pass
    receipt.update(status="owned_turn_termination_pending", turn_id=turn_id,
                   interrupt_request_state=request_state, termination_confirmed=False,
                   recorded_at=utc())
    store(receipt)
    terminal = await _await_owned_terminal(ws, rpc, deadline, turn_id, pacing)
    if terminal is not None:
        receipt["termination_confirmed"] = True
    return terminal


async def _recover_prior_timed_out_tick(rpc: RPC, deadline: TickDeadline,
                                        receipt: dict,
                                        pacing: dict) -> int | None:
    """Act only on the previously persisted nonce plus compatible exact ID."""
    previous = STATE / "scheduler-wakeup-previous.json"
    old = json.loads(previous.read_text()) if previous.exists() else {}
    old_nonce = old.get("client_message_id")
    old_started = old.get("started_at")
    if not old_nonce or not old_started:
        return
    elapsed = (datetime.now(timezone.utc)
               - datetime.fromisoformat(old_started)).total_seconds()
    if elapsed < TICK_DEADLINE_SECONDS:
        return
    turn = await _find_nonce_turn(rpc, deadline, old_nonce)
    if turn is None:
        return
    if old.get("turn_id") and turn["id"] != old["turn_id"]:
        # Identity conflict: never interrupt an unrelated active turn.
        return
    if turn.get("status") == "inProgress":
        receipt.update(client_message_id=old_nonce, started_at=old_started,
                       recovery_of_previous_tick=True)
        terminal = await _interrupt_and_wait(rpc.ws, rpc, deadline, receipt,
                                             turn["id"], pacing)
        if terminal is not None:
            return _finish(receipt, terminal)
        receipt.update(status="previous_tick_termination_unconfirmed", recorded_at=utc())
        store(receipt)
        return 1
    return None


async def _attempt_and_supervise(args, prompt: str, receipt: dict, ws, rpc: RPC,
                                 deadline: TickDeadline, message_id: str,
                                 goal_status: str | None,
                                 pacing: dict) -> int:
    start_request_id = rpc.next_id()
    accepted_turn_id = None
    try:
        result = await rpc.call("turn/start", {
            "threadId": THREAD,
            "input": [{"type": "text", "text": prompt}],
            "clientUserMessageId": message_id,
            "model": args.model,
            "effort": args.effort,
            "cwd": str(ROOT),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }, deadline, request_id=start_request_id)
        accepted_turn_id = (result.get("turn") or {}).get("id")
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
        # A lost/rejected start reply never authorizes a second start; the turn
        # may already be accepted on the server.
        accepted_turn_id = None
    late = rpc.late_results.pop(start_request_id, None)
    if accepted_turn_id is None and late is not None and "error" not in late:
        # A late start response with the original request id supplies the ID.
        accepted_turn_id = (late.get("result", {}).get("turn") or {}).get("id")
    if accepted_turn_id is not None:
        # Persist the accepted turn ID before the first ownership read.
        receipt.update(status="start_accepted", turn_id=accepted_turn_id,
                       recorded_at=utc())
        store(receipt)

    owned_turn_id = None
    goal_verified = False
    warned = False
    conflict_recorded = False
    while True:
        # Reserve the final window for the exact-owned status check/interrupt.
        if deadline.exhausted() \
                or deadline.remaining() <= CLEANUP_WINDOW_SECONDS:
            break
        terminal = _drain_terminal_event(rpc, owned_turn_id)
        if terminal is not None:
            return _finish(receipt, terminal)
        try:
            if owned_turn_id is None:
                state, owned_turn_id = await reconcile_ownership(
                    rpc, deadline, message_id, accepted_turn_id)
                if state == "owned":
                    receipt.update(status="running", turn_id=owned_turn_id,
                                   recorded_at=utc())
                    store(receipt)
                    if not goal_verified:
                        saved = (await rpc.call("thread/goal/get",
                                                {"threadId": THREAD}, deadline)).get("goal")
                        if (saved.get("status") if saved else None) != goal_status:
                            raise RuntimeError("turn start changed the saved goal status")
                        goal_verified = True
                elif state == "conflict" and not conflict_recorded:
                    # Conflicting identities remain unresolved; no steering or
                    # interruption of an unproven turn.
                    conflict_recorded = True
                    receipt.update(status="start_ownership_unresolved",
                                   turn_id=accepted_turn_id, recorded_at=utc())
                    store(receipt)
            else:
                status = await _owned_turn_status(rpc, deadline, owned_turn_id)
                if status in TERMINAL_STATUSES:
                    return _finish(receipt, status)
                if not warned and _now() - deadline.started >= ADVISORY_SECONDS:
                    # Consume the advisory-attempt flag before dispatching, so a
                    # failed send, rejected response, lost reply, or reconnect can
                    # never dispatch the advisory a second time.
                    warned = True
                    try:
                        await rpc.call("turn/steer", {
                            "threadId": THREAD, "expectedTurnId": owned_turn_id,
                            "input": [{"type": "text", "text":
                                "This scheduled tick is at minute 12. Reconcile current "
                                "action receipts, persist a truthful closing observation, "
                                "report visibly in this chat, and finish now. Do not begin "
                                "another action."}]}, deadline)
                    except RuntimeError:
                        # Completion can win the race with this advisory request.
                        pass
        except TickDeadlineExceeded:
            break
        except RuntimeError as exc:
            if not _is_rpc_rejection(exc):
                # Explicit host/thread/goal invariant failures are never swallowed
                # or treated as ownership proof.
                raise
            # A rejected ownership/status read is uncertainty, never completion or
            # absence: retain the original nonce/accepted ID/start intent (or the
            # proven-owned turn) and continue bounded observation on the existing
            # retry intervals. Never interrupt an unproven turn.
            pass
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
            # Reconnect and RPC exceptions preserve ownership and timing receipts
            # rather than dropping into immediate outer failure.
            receipt.update(status="transport_reconnecting",
                           turn_id=owned_turn_id or accepted_turn_id,
                           recorded_at=utc())
            store(receipt)
            ws, rpc = await reconnect(ws, deadline, pacing)
        await _sleep(_retry_interval(deadline))

    # Final cleanup window: prefer buffered/fresh terminal evidence.
    terminal = _drain_terminal_event(rpc, owned_turn_id)
    if terminal is None and owned_turn_id is not None:
        try:
            status = await _owned_turn_status(rpc, deadline, owned_turn_id)
            if status in TERMINAL_STATUSES:
                terminal = status
        except (OSError, asyncio.TimeoutError, RuntimeError,
                websockets.exceptions.WebSocketException, TickDeadlineExceeded):
            # A rejected final cleanup read must not escape before the single
            # exact-owned interrupt/terminal-observation path.
            terminal = None
    if terminal is not None:
        return _finish(receipt, terminal)
    if owned_turn_id is not None:
        terminal = await _interrupt_and_wait(ws, rpc, deadline, receipt,
                                             owned_turn_id, pacing)
        if terminal is not None:
            return _finish(receipt, terminal)
    if receipt.get("interrupt_request_state") == "acknowledged":
        receipt.update(status="interrupt_requested_at_tick_deadline",
                       turn_id=owned_turn_id, client_message_id=message_id,
                       recorded_at=utc())
        store(receipt)
        return 1
    if owned_turn_id is not None:
        receipt.update(status="tick_unresolved_at_deadline", turn_id=owned_turn_id,
                       client_message_id=message_id, recorded_at=utc())
    else:
        receipt.update(status="ownership_unresolved_at_deadline",
                       turn_id=accepted_turn_id, client_message_id=message_id,
                       recorded_at=utc())
    store(receipt)
    return 1


async def _observe(args, prompt: str, receipt: dict, ws, rpc: RPC,
                   deadline: TickDeadline, pacing: dict) -> int:
    thread = (await rpc.call("thread/read",
                             {"threadId": THREAD, "includeTurns": False},
                             deadline))["thread"]
    if thread["id"] != THREAD or Path(thread["cwd"]) != ROOT:
        raise RuntimeError("pinned chat identity or checkout mismatch")
    receipt["thread_status"] = thread["status"]["type"]
    if args.inspect_only:
        goal = (await rpc.call("thread/goal/get", {"threadId": THREAD},
                               deadline)).get("goal")
        receipt["saved_goal_status"] = goal.get("status") if goal else None
        receipt["status"] = "read_only_route_verified"
        store(receipt)
        print(json.dumps(receipt))
        return 0
    if thread["status"]["type"] == "active":
        recovered = await _recover_prior_timed_out_tick(rpc, deadline, receipt,
                                                        pacing)
        if recovered is not None:
            return recovered
        receipt["status"] = "skipped_existing_active_turn"
        store(receipt)
        return 0
    if thread["status"]["type"] not in ("idle", "notLoaded"):
        raise RuntimeError("chat is not available for a scheduled turn")
    goal_before = (await rpc.call("thread/goal/get", {"threadId": THREAD},
                                  deadline)).get("goal")
    # Resume on the SAME server to subscribe, without hydrating the conversation.
    await rpc.call("thread/resume", {"threadId": THREAD, "excludeTurns": True}, deadline)
    goal_after = (await rpc.call("thread/goal/get", {"threadId": THREAD},
                                 deadline)).get("goal")
    before = goal_before.get("status") if goal_before else None
    after = goal_after.get("status") if goal_after else None
    if before != after:
        raise RuntimeError("resume changed the saved goal status; no tick started")
    if after == "active":
        raise RuntimeError("unexpected active autonomous goal; no tick started")
    if _now() - deadline.started >= START_ADMISSION_CUTOFF_SECONDS:
        receipt.update(status="skipped_start_deadline", recorded_at=utc())
        store(receipt)
        return 1
    message_id = _new_nonce()
    receipt.update(status="turn_start_intent", recorded_at=utc(),
                   client_message_id=message_id, started_at=utc(),
                   saved_goal_status=after)
    store(receipt)
    return await _attempt_and_supervise(args, prompt, receipt, ws, rpc, deadline,
                                        message_id, after, pacing)


async def observe_or_run(args, prompt: str, started: float) -> int:
    deadline = TickDeadline(started)
    receipt = {"recorded_at": utc(), "thread_id": THREAD, "model": args.model,
               "effort": args.effort, "route": "existing_app_server",
               "status": "inspecting", "wakeup_id": args.wakeup_id}
    store(receipt)
    ws, rpc = await open_connection(deadline)
    pacing = {"last_attempt": _now()}
    try:
        return await _observe(args, prompt, receipt, ws, rpc, deadline, pacing)
    except TickDeadlineExceeded:
        receipt.update(status="ownership_unresolved_at_deadline",
                       turn_id=receipt.get("turn_id"),
                       client_message_id=receipt.get("client_message_id"),
                       recorded_at=utc())
        store(receipt)
        return 1
    finally:
        if deadline.remaining() > 0:
            try:
                await asyncio.wait_for(ws.close(), min(2.0, deadline.remaining()))
            except Exception:
                pass


def _stop_process_group(proc: subprocess.Popen, deadline: TickDeadline) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=min(2.0, max(0.1, deadline.remaining())))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def run_cli_fallback(args, prompt: str, deadline: TickDeadline) -> int:
    """No-socket exact-chat fallback: supervised foreground child, same lock."""
    receipt = {"recorded_at": utc(), "thread_id": THREAD, "model": args.model,
               "effort": args.effort, "route": "exact_chat_cli_fallback",
               "status": "cli_fallback_no_server", "wakeup_id": args.wakeup_id}
    store(receipt)
    wait_budget = deadline.remaining() - CLEANUP_WINDOW_SECONDS
    if wait_budget <= 0:
        receipt.update(status="cli_fallback_budget_exhausted", recorded_at=utc())
        store(receipt)
        return 1
    command = [CLI_FALLBACK, "exec", "resume",
               "--dangerously-bypass-approvals-and-sandbox",
               "--model", args.model,
               "--config", f'model_reasoning_effort="{args.effort}"',
               "--output-last-message", str(STATE / "last-message.md"),
               THREAD, "-"]
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=str(ROOT), start_new_session=True)
    except OSError as exc:
        receipt.update(status="cli_fallback_launch_failed",
                       error_type=type(exc).__name__, recorded_at=utc())
        store(receipt)
        return 1
    try:
        proc.communicate(input=prompt.encode(), timeout=wait_budget)
    except subprocess.TimeoutExpired:
        _stop_process_group(proc, deadline)
        receipt.update(status="cli_fallback_expired", recorded_at=utc())
        store(receipt)
        return 1
    code = proc.returncode
    receipt.update(status="cli_fallback_completed" if code == 0 else "cli_fallback_failed",
                   exit_code=code, recorded_at=utc())
    store(receipt)
    return 0 if code == 0 else 1


def _service_start_monotonic(started: float) -> float | None:
    """Adopt the oneshot service start (activating, or active with a valid ts)."""
    try:
        info = subprocess.check_output(
            ["systemctl", "show", "aflow-concierge.service", "-p", "ActiveState",
             "-p", "ExecMainStartTimestampMonotonic"],
            text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
    if fields.get("ActiveState") not in ("activating", "active"):
        return None
    try:
        candidate = int(fields.get("ExecMainStartTimestampMonotonic", "")) / 1_000_000
    except ValueError:
        return None
    if 0 < candidate <= started:
        return candidate
    return None


def _lease_skip(args) -> int | None:
    mandate = json.loads((STATE / "mandate.json").read_text())
    lease = mandate.get("chat_coordination_lease_until")
    if args.inspect_only:
        return None
    if lease and datetime.fromisoformat(lease) > datetime.now(timezone.utc):
        store({"recorded_at": utc(), "thread_id": THREAD,
               "status": "skipped_valid_chat_coordination_lease",
               "lease_until": lease, "schedule_remains_enabled": True,
               "wakeup_id": args.wakeup_id})
        return 0
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wake the pinned concierge chat (p100 host adapter).")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    if args.thread != THREAD or Path.cwd() != ROOT or os.uname().nodename != "codex":
        raise RuntimeError("concierge host, checkout or pinned chat mismatch")
    started = _now()
    # Include uv/launcher startup in the scheduled service's deadline.
    if not args.inspect_only:
        candidate = _service_start_monotonic(started)
        if candidate is not None:
            started = candidate
    deadline = TickDeadline(started)
    args.wakeup_id = str(uuid.uuid4())
    previous = STATE / "scheduler-wakeup-latest.json"
    if previous.exists():
        store(json.loads(previous.read_text()), "scheduler-wakeup-previous.json")
    prompt = sys.stdin.read()
    if not prompt.strip() and not args.inspect_only:
        raise RuntimeError("missing scheduled coordinator prompt")
    lease_result = _lease_skip(args)
    if lease_result is not None:
        return lease_result
    if not SOCKET.exists() and not args.inspect_only:
        return run_cli_fallback(args, prompt, deadline)
    try:
        return asyncio.run(observe_or_run(args, prompt, started))
    except Exception as exc:
        # No credentials, prompts, server exception text, events, or model
        # output enter operational records.
        prior = json.loads(previous.read_text()) if previous.exists() else {}
        receipt = prior if prior.get("wakeup_id") == args.wakeup_id else {}
        receipt.update(recorded_at=utc(), thread_id=THREAD,
                       failure_stage=receipt.get("status"), status="wakeup_failed",
                       error_type=type(exc).__name__, wakeup_id=args.wakeup_id)
        store(receipt)
        print(f"concierge wakeup failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
