"""Deterministic tests for the p100 persistent-chat concierge host adapter.

Every scenario runs against a real temporary Unix WebSocket server using the
production RPC serialization/receive code from scripts/concierge/codex_chat_tick.py,
with an injected clock. The real host socket, service, auth, and state are never
touched; the CLI fallback uses a harmless fake child only.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "concierge" / "codex_chat_tick.py"
PROMPT = "sanitized test prompt CANARY-PROMPT-SECRET-abc123"
NONCE = "nonce-canary-0123456789"
OWNED_TURN = "cron-turn"


def _load_module():
    spec = importlib.util.spec_from_file_location("codex_chat_tick", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    value: float

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += max(0.0, seconds)


def _user_message(client_id: str) -> dict:
    return {"item": {"type": "userMessage", "id": f"item-{client_id}",
                     "clientId": client_id, "content": ""}}


class FakeCodex:
    """Scripted app-server peer speaking the production wire protocol."""

    def __init__(self, tick, thread_status: str = "idle",
                 goal_status: str | None = "paused",
                 start_turn_id: str = OWNED_TURN,
                 turns: list | None = None,
                 items: dict | None = None,
                 items_delayed_until: int = 0,
                 item_pages: dict | None = None,
                 turn_status_fn=None,
                 events_after_start: list | None = None,
                 drop_start_reply: bool = False,
                 close_after_start: bool = False,
                 late_start_reply: bool = False,
                 reject_interrupt: bool = False) -> None:
        self.tick = tick
        self.thread_status = thread_status
        self.goal_status = goal_status
        self.start_turn_id = start_turn_id
        self.turns = turns if turns is not None else [
            {"id": start_turn_id, "status": "inProgress"}]
        self.items = items if items is not None else {
            start_turn_id: [_user_message(NONCE)]}
        self.items_delayed_until = items_delayed_until
        self.item_pages = item_pages or {}
        self.turn_status_fn = turn_status_fn
        self.events_after_start = events_after_start or []
        self.drop_start_reply = drop_start_reply
        self.close_after_start = close_after_start
        self.late_start_reply = late_start_reply
        self.reject_interrupt = reject_interrupt
        self.calls: list = []
        self.starts: list = []
        self.interrupts: list = []
        self.steers: list = []
        self.items_calls: dict = {}
        self.item_page_index: dict = {}
        self.connections = 0
        self.complete_after_interrupt = False

    async def _reply(self, ws, rid, result) -> None:
        await ws.send(json.dumps({"id": rid, "result": result}))

    async def serve(self, ws) -> None:
        self.connections += 1
        async for raw in ws:
            await self.handle_one(ws, json.loads(raw))

    async def handle_one(self, ws, req) -> None:
        method = req.get("method")
        params = req.get("params", {})
        rid = req.get("id")
        if rid is None:
            return  # client notification (initialized)
        self.calls.append((method, params))
        if method == "initialize":
            await self._reply(ws, rid, {})
        elif method == "thread/read":
            await self._reply(ws, rid, {"thread": {
                "id": self.tick.THREAD, "cwd": str(self.tick.ROOT),
                "status": {"type": self.thread_status}}})
        elif method == "thread/resume":
            await self._reply(ws, rid, {"thread": {"id": self.tick.THREAD}})
        elif method == "thread/goal/get":
            status = self.goal_status() if callable(self.goal_status) \
                else self.goal_status
            await self._reply(ws, rid,
                              {"goal": {"status": status}} if status else {})
        elif method == "turn/start":
            self.starts.append(params)
            if self.late_start_reply:
                await ws.send(json.dumps({
                    "method": "thread/updated",
                    "params": {"threadId": self.tick.THREAD}}))
            if self.drop_start_reply:
                if self.close_after_start:
                    await ws.close()
                    return
            else:
                await self._reply(ws, rid,
                                  {"turn": {"id": self.start_turn_id}})
            for event in self.events_after_start:
                await ws.send(json.dumps(event))
        elif method == "thread/turns/list":
            data = []
            for turn in self.turns:
                entry = dict(turn)
                if self.turn_status_fn is not None:
                    entry["status"] = self.turn_status_fn()
                data.append(entry)
            await self._reply(ws, rid, {"data": data})
        elif method == "thread/items/list":
            turn_id = params["turnId"]
            self.items_calls[turn_id] = self.items_calls.get(turn_id, 0) + 1
            pages = self.item_pages.get(turn_id)
            if pages is not None:
                # Stateless, cursor-driven paging: the cursor value itself
                # selects the page, so a client may restart a scan from
                # page 0 on any connection or pass.
                cursor = params.get("cursor")
                if cursor is None:
                    index = 0
                else:
                    prefix = f"{turn_id}:p"
                    assert cursor.startswith(prefix), \
                        f"bad cursor {cursor!r}"
                    index = int(cursor[len(prefix):])
                assert index < len(pages), f"cursor beyond last page {cursor!r}"
                page = pages[index]
                reply = {"data": page}
                if index + 1 < len(pages):
                    reply["next_cursor"] = f"{turn_id}:p{index + 1}"
                await self._reply(ws, rid, reply)
            elif self.items_calls[turn_id] < self.items_delayed_until:
                await self._reply(ws, rid, {"data": []})
            else:
                await self._reply(ws, rid,
                                  {"data": self.items.get(turn_id, [])})
        elif method == "turn/interrupt":
            self.interrupts.append(params)
            if self.reject_interrupt:
                self.complete_after_interrupt = True
                await ws.send(json.dumps(
                    {"id": rid, "error": {"code": -32000,
                                          "message": "CANARY-ERR no such turn"}}))
            else:
                await self._reply(ws, rid, {})
        elif method == "turn/steer":
            self.steers.append(params)
            await self._reply(ws, rid, {})
        else:
            raise AssertionError(f"unexpected method {method}")


def _args(tick, inspect_only: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(model="gpt-6.1-sol", effort="xhigh",
                                 inspect_only=inspect_only, wakeup_id="test-wakeup")


def _methods(fake: FakeCodex) -> list:
    return [method for method, _ in fake.calls]


def _old_started(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class TickTestCase(unittest.IsolatedAsyncioTestCase):
    """Base: loads the adapter once and isolates state/socket/clock per test."""

    @classmethod
    def setUpClass(cls):
        cls.tick = _load_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.clock = FakeClock()
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "mandate.json").write_text("{}")
        self.socket_path = tmp_path / "server.sock"
        self._orig = {name: getattr(self.tick, name)
                      for name in ("STATE", "SOCKET", "_now", "_sleep", "_recv",
                                   "_new_nonce")}
        self.tick.STATE = self.state
        self.tick.SOCKET = self.socket_path
        self.tick._now = self.clock.now
        self.tick._sleep = self.clock.sleep
        self.tick._new_nonce = lambda: NONCE
        self._receipt_writes: list = []
        self._orig_store = self.tick.store

        def recording(value, filename="scheduler-wakeup-latest.json"):
            self._orig_store(value, filename)
            self._receipt_writes.append(dict(value))

        self.tick.store = recording

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(self.tick, name, value)
        self.tick.store = self._orig_store
        self._tmp.cleanup()

    async def run_tick(self, fake: FakeCodex, inspect_only: bool = False,
                       prompt: str = PROMPT, started_offset: float = 0.0):
        args = _args(self.tick, inspect_only=inspect_only)
        async with websockets.unix_serve(fake.serve, str(self.socket_path)):
            code = await self.tick.observe_or_run(
                args, prompt, self.clock.value - started_offset)
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        return code, receipt, fake

    def status_writes(self) -> list:
        return [w["status"] for w in self._receipt_writes]


# --- acceptance 1: delayed ownership visibility ---------------------------------

class DelayedVisibilityTests(TickTestCase):
    async def test_delayed_visibility_proves_ownership_and_completes(self):
        fake = FakeCodex(self.tick, items_delayed_until=3)
        fake.turn_status_fn = lambda: "completed" \
            if fake.items_calls.get(OWNED_TURN, 0) >= 3 else "inProgress"
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertEqual(len(fake.starts), 1)
        self.assertEqual(fake.starts[0]["clientUserMessageId"], NONCE)
        self.assertFalse(fake.interrupts)
        statuses = self.status_writes()
        self.assertLess(statuses.index("turn_start_intent"),
                        statuses.index("start_accepted"))
        self.assertLess(statuses.index("start_accepted"),
                        statuses.index("running"))
        accepted = next(w for w in self._receipt_writes
                        if w["status"] == "start_accepted")
        self.assertEqual(accepted["turn_id"], OWNED_TURN)
        intent = next(w for w in self._receipt_writes
                      if w["status"] == "turn_start_intent")
        self.assertEqual(intent["client_message_id"], NONCE)


# --- acceptance 2: buffered completion before nonce visibility ------------------

class BufferedCompletionTests(TickTestCase):
    async def test_completion_event_buffered_until_nonce_proof(self):
        fake = FakeCodex(self.tick, items_delayed_until=3, events_after_start=[
            {"method": "turn/completed",
             "params": {"threadId": self.tick.THREAD,
                        "turn": {"id": OWNED_TURN, "status": "completed"}}},
            {"method": "turn/completed",
             "params": {"threadId": self.tick.THREAD,
                        "turn": {"id": "other-turn", "status": "completed"}}},
            {"method": "turn/completed",
             "params": {"threadId": "other-thread",
                        "turn": {"id": OWNED_TURN, "status": "completed"}}},
        ])
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(fake.interrupts)
        self.assertEqual(len(fake.starts), 1)


# --- acceptance 3: owner-active turn and start race ------------------------------

class ActiveTurnTests(TickTestCase):
    async def test_active_manual_turn_is_skipped_without_mutation(self):
        fake = FakeCodex(self.tick, thread_status="active")
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "skipped_existing_active_turn")
        for method in ("turn/start", "turn/interrupt", "turn/steer"):
            self.assertNotIn(method, _methods(fake))

    async def test_start_race_without_nonce_never_interrupts(self):
        fake = FakeCodex(self.tick,
                         items={OWNED_TURN: [_user_message("desktop-owner-message")]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ownership_unresolved_at_deadline")
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertEqual(len(fake.starts), 1)
        self.assertFalse(fake.interrupts)
        self.assertFalse(fake.steers)

    async def test_conflicting_accepted_id_and_nonce_match_stays_unresolved(self):
        fake = FakeCodex(self.tick,
                         turns=[{"id": OWNED_TURN, "status": "inProgress"},
                                {"id": "other-turn", "status": "inProgress"}],
                         items={OWNED_TURN: [_user_message("desktop-owner-message")],
                                "other-turn": [_user_message(NONCE)]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ownership_unresolved_at_deadline")
        self.assertIn("start_ownership_unresolved", self.status_writes())
        self.assertFalse(fake.interrupts)
        self.assertFalse(fake.steers)
        self.assertEqual(len(fake.starts), 1)


# --- acceptance 4: lost start reply and reconnect --------------------------------

class LostReplyTests(TickTestCase):
    async def test_lost_start_reply_reconnects_and_supervises_same_turn(self):
        fake = FakeCodex(self.tick, drop_start_reply=True, close_after_start=True,
                         turns=[{"id": OWNED_TURN, "status": "completed"}])
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertEqual(len(fake.starts), 1)
        self.assertEqual(fake.connections, 2)
        self.assertFalse(fake.interrupts)
        self.assertIn("transport_reconnecting", self.status_writes())

    async def test_late_start_reply_supplies_accepted_id(self):
        fake = FakeCodex(self.tick, late_start_reply=True,
                         events_after_start=[
                             {"method": "turn/completed",
                              "params": {"threadId": self.tick.THREAD,
                                         "turn": {"id": OWNED_TURN,
                                                  "status": "completed"}}}])
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        accepted = next(w for w in self._receipt_writes
                        if w["status"] == "start_accepted")
        self.assertEqual(accepted["turn_id"], OWNED_TURN)
        self.assertEqual(len(fake.starts), 1)
        self.assertFalse(fake.interrupts)


# --- acceptance 5: pagination and bounded uncertainty ----------------------------

class PaginationTests(TickTestCase):
    async def test_nonce_beyond_first_page_is_found_through_cursor(self):
        fake = FakeCodex(self.tick, item_pages={
            OWNED_TURN: [[{"item": {"type": "systemMessage", "id": "s1"}}],
                         [_user_message(NONCE)]],
        })
        fake.turn_status_fn = lambda: "completed" \
            if fake.items_calls.get(OWNED_TURN, 0) >= 2 else "inProgress"
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertFalse(fake.interrupts)

    async def test_page_cap_exhaustion_never_grants_ownership(self):
        filler = [{"item": {"type": "systemMessage", "id": f"s{i}"}}
                  for i in range(16)]
        fake = FakeCodex(self.tick,
                         item_pages={OWNED_TURN: [filler[:1] for _ in range(12)]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "ownership_unresolved_at_deadline")
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertFalse(fake.interrupts)
        self.assertEqual(len(fake.starts), 1)
        self.assertGreater(fake.items_calls.get(OWNED_TURN, 0), 0)


# --- acceptance 6: cleanup-window completion and interrupt races -----------------

class CleanupWindowTests(TickTestCase):
    async def test_completion_in_cleanup_window_reports_success(self):
        deadline = self.clock.value + self.tick.TICK_DEADLINE_SECONDS
        fake = FakeCodex(self.tick, turn_status_fn=lambda:
                         "completed" if self.clock.value > deadline - 6
                         else "inProgress")
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(fake.interrupts)
        self.assertEqual(len(fake.steers), 1)
        self.assertEqual(fake.steers[0]["expectedTurnId"], OWNED_TURN)

    async def test_owned_active_turn_interrupted_exactly_once(self):
        deadline_at = self.clock.value + self.tick.TICK_DEADLINE_SECONDS
        fake = FakeCodex(self.tick)
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "interrupt_requested_at_tick_deadline")
        self.assertEqual(receipt["turn_id"], OWNED_TURN)
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual([p["turnId"] for p in fake.interrupts], [OWNED_TURN])
        self.assertEqual(len(fake.steers), 1)
        self.assertEqual(self.clock.value, deadline_at)
        self.assertEqual(receipt["interrupt_request_state"], "acknowledged")
        self.assertFalse(receipt["termination_confirmed"])

    async def test_terminal_after_interrupt_acknowledgment_reports_actual_result(self):
        for status in ("completed", "interrupted", "failed"):
            with self.subTest(status=status):
                deadline_at = self.clock.value + self.tick.TICK_DEADLINE_SECONDS
                fake = FakeCodex(self.tick)
                fake.turn_status_fn = lambda: status if fake.interrupts and \
                    self.clock.value >= deadline_at - 2 else "inProgress"
                code, receipt, fake = await self.run_tick(fake)
                self.assertEqual(code, 0 if status == "completed" else 1)
                self.assertEqual(receipt["status"], status)
                self.assertTrue(receipt["termination_confirmed"])
                self.assertLess(self.clock.value, deadline_at)
                self.assertEqual([p["turnId"] for p in fake.interrupts], [OWNED_TURN])

    async def test_lost_interrupt_reply_reconnects_without_second_interrupt(self):
        class LostInterruptReply(FakeCodex):
            async def handle_one(self, ws, req):
                if req.get("method") == "turn/interrupt":
                    self.interrupts.append(req["params"])
                    await ws.close()
                    return
                await super().handle_one(ws, req)

        deadline_at = self.clock.value + self.tick.TICK_DEADLINE_SECONDS
        fake = LostInterruptReply(self.tick)
        fake.turn_status_fn = lambda: "completed" if fake.interrupts and \
            self.clock.value >= deadline_at - 2 else "inProgress"
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["termination_confirmed"])
        self.assertEqual([p["turnId"] for p in fake.interrupts], [OWNED_TURN])
        self.assertEqual(fake.connections, 2)

    async def test_interrupt_rejection_with_terminal_evidence_completes(self):
        fake = FakeCodex(self.tick, reject_interrupt=True)
        fake.turn_status_fn = lambda: "completed" \
            if fake.complete_after_interrupt else "inProgress"
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual([p["turnId"] for p in fake.interrupts], [OWNED_TURN])


# --- acceptance 7: bounded deadline and admission cutoff --------------------------

class DeadlineTests(TickTestCase):
    async def test_hanging_transport_cannot_extend_deadline(self):
        clock = self.clock

        async def bounded_recv(ws, timeout):
            started_at = time.monotonic()
            try:
                return await asyncio.wait_for(ws.recv(), min(timeout, 0.05))
            except asyncio.TimeoutError:
                # A hung read consumes the fake budget for its full bound.
                clock.value += timeout
                raise
            clock.value += time.monotonic() - started_at

        self.tick._recv = bounded_recv
        silent = FakeCodex(self.tick)

        async def serve(ws):
            async for raw in ws:
                req = json.loads(raw)
                if req.get("method") == "thread/items/list":
                    continue  # item reads hang forever
                await silent.handle_one(ws, req)

        async with websockets.unix_serve(serve, str(self.socket_path)):
            code = await self.tick.observe_or_run(
                _args(self.tick), PROMPT, clock.value)
        self.assertEqual(code, 1)
        self.assertLessEqual(clock.value - 1_000.0, 780.0 + 1.0)
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        self.assertEqual(receipt["status"], "ownership_unresolved_at_deadline")
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertFalse(silent.interrupts)
        self.assertEqual(len(silent.starts), 1)

    async def test_admission_cutoff_makes_zero_starts(self):
        fake = FakeCodex(self.tick)
        code, receipt, fake = await self.run_tick(fake, started_offset=601.0)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "skipped_start_deadline")
        self.assertFalse(fake.starts)
        self.assertNotIn("turn/start", _methods(fake))

    def test_deadline_is_single_and_start_timestamp_bounded(self):
        deadline = self.tick.TickDeadline(started=100.0)
        self.assertEqual(deadline.deadline, 100.0 + 780.0)
        self.assertEqual(deadline.started, 100.0)

    def test_service_start_monotonic_adoption(self):
        def fake_output(text):
            def run(*a, **k):
                return text
            return run

        original = self.tick.subprocess.check_output
        try:
            self.tick.subprocess.check_output = fake_output(
                "ActiveState=activating\n"
                "ExecMainStartTimestampMonotonic=5000000\n")
            self.assertEqual(self.tick._service_start_monotonic(10.0), 5.0)
            self.tick.subprocess.check_output = fake_output(
                "ActiveState=active\n"
                "ExecMainStartTimestampMonotonic=5000000\n")
            self.assertEqual(self.tick._service_start_monotonic(10.0), 5.0)
            self.tick.subprocess.check_output = fake_output(
                "ActiveState=inactive\n"
                "ExecMainStartTimestampMonotonic=5000000\n")
            self.assertIsNone(self.tick._service_start_monotonic(10.0))
            self.tick.subprocess.check_output = fake_output(
                "ActiveState=activating\n"
                "ExecMainStartTimestampMonotonic=999999999999999\n")
            self.assertIsNone(self.tick._service_start_monotonic(10.0))
        finally:
            self.tick.subprocess.check_output = original


# --- acceptance 8: prior tick recovery, goal, lease, inspect-only ----------------

class RecoveryAndGuardsTests(TickTestCase):
    async def test_prior_timed_out_tick_interrupted_by_stored_nonce_and_id(self):
        (self.state / "scheduler-wakeup-previous.json").write_text(json.dumps({
            "client_message_id": "old-nonce", "turn_id": "old-turn",
            "started_at": _old_started(800.0)}))
        fake = FakeCodex(self.tick, thread_status="active",
                         turns=[{"id": "old-turn", "status": "inProgress"},
                                {"id": "other-turn", "status": "inProgress"}],
                         items={"old-turn": [_user_message("old-nonce")]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "previous_tick_termination_unconfirmed")
        self.assertEqual(receipt["interrupt_request_state"], "acknowledged")
        self.assertFalse(receipt["termination_confirmed"])
        self.assertTrue(receipt["recovery_of_previous_tick"])
        self.assertEqual(receipt["turn_id"], "old-turn")
        self.assertEqual([p["turnId"] for p in fake.interrupts], ["old-turn"])
        self.assertFalse(fake.starts)

    async def test_unrelated_active_owner_turn_stays_untouched(self):
        (self.state / "scheduler-wakeup-previous.json").write_text(json.dumps({
            "client_message_id": "old-nonce", "turn_id": "old-turn",
            "started_at": _old_started(800.0)}))
        fake = FakeCodex(self.tick, thread_status="active",
                         turns=[{"id": "manual-turn", "status": "inProgress"}],
                         items={"manual-turn": [_user_message("owner-message")]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "skipped_existing_active_turn")
        self.assertFalse(fake.interrupts)
        self.assertFalse(fake.starts)

    async def test_prior_tick_identity_conflict_not_interrupted(self):
        (self.state / "scheduler-wakeup-previous.json").write_text(json.dumps({
            "client_message_id": "old-nonce", "turn_id": "old-turn",
            "started_at": _old_started(800.0)}))
        fake = FakeCodex(self.tick, thread_status="active",
                         turns=[{"id": "manual-turn", "status": "inProgress"}],
                         items={"manual-turn": [_user_message("old-nonce")]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "skipped_existing_active_turn")
        self.assertFalse(fake.interrupts)

    async def test_recent_prior_tick_not_interrupted(self):
        (self.state / "scheduler-wakeup-previous.json").write_text(json.dumps({
            "client_message_id": "old-nonce", "turn_id": "old-turn",
            "started_at": _old_started(100.0)}))
        fake = FakeCodex(self.tick, thread_status="active",
                         turns=[{"id": "old-turn", "status": "inProgress"}],
                         items={"old-turn": [_user_message("old-nonce")]})
        code, receipt, fake = await self.run_tick(fake)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "skipped_existing_active_turn")
        self.assertFalse(fake.interrupts)

    async def test_active_goal_blocks_start(self):
        fake = FakeCodex(self.tick, goal_status="active")
        with self.assertRaisesRegex(RuntimeError, "goal"):
            await self.run_tick(fake)
        self.assertFalse(fake.starts)

    async def test_goal_status_change_blocks_start(self):
        statuses = iter(["paused", "active"])
        fake = FakeCodex(self.tick, goal_status=lambda: next(statuses))
        with self.assertRaisesRegex(RuntimeError, "goal"):
            await self.run_tick(fake)
        self.assertFalse(fake.starts)

    async def test_inspect_only_makes_zero_model_starts(self):
        fake = FakeCodex(self.tick)
        code, receipt, fake = await self.run_tick(fake, inspect_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "read_only_route_verified")
        self.assertEqual(receipt["saved_goal_status"], "paused")
        for method in ("turn/start", "thread/resume", "turn/interrupt"):
            self.assertNotIn(method, _methods(fake))

    def test_valid_chat_coordination_lease_skips(self):
        tick = self.tick
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (self.state / "mandate.json").write_text(
            json.dumps({"chat_coordination_lease_until": future}))
        args = _args(tick)
        self.assertEqual(tick._lease_skip(args), 0)
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        self.assertEqual(receipt["status"],
                         "skipped_valid_chat_coordination_lease")
        self.assertTrue(receipt["schedule_remains_enabled"])
        (self.state / "mandate.json").write_text("{}")
        self.assertIsNone(tick._lease_skip(args))


# --- acceptance 9: supervised CLI fallback ----------------------------------------

class CliFallbackTests(TickTestCase):
    def _write_child(self, body: str) -> Path:
        child = Path(self._tmp.name) / "fake-codex"
        child.write_text("#!/bin/sh\n" + body)
        child.chmod(0o755)
        return child

    def test_fallback_child_runs_same_chat_args_and_stdin(self):
        tick = self.tick
        out = Path(self._tmp.name) / "child-args.txt"
        child = self._write_child(
            f'cat > "{out}"\nprintf "%s\\n" "$@" >> "{out}"\n')
        original = tick.CLI_FALLBACK
        tick.CLI_FALLBACK = str(child)
        try:
            code = tick.run_cli_fallback(_args(tick), PROMPT,
                                         tick.TickDeadline(self.clock.value))
        finally:
            tick.CLI_FALLBACK = original
        self.assertEqual(code, 0)
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        self.assertEqual(receipt["status"], "cli_fallback_completed")
        recorded = out.read_text()
        self.assertIn(PROMPT, recorded)
        self.assertIn(tick.THREAD, recorded)
        self.assertIn('model_reasoning_effort="xhigh"', recorded)

    def test_fallback_child_reaped_within_remaining_budget(self):
        tick = self.tick
        child = self._write_child("sleep 60\n")
        original = tick.CLI_FALLBACK
        tick.CLI_FALLBACK = str(child)
        try:
            deadline = tick.TickDeadline(self.clock.value)
            self.clock.value += 770.0  # 10s remain
            began = time.monotonic()
            code = tick.run_cli_fallback(_args(tick), PROMPT, deadline)
            elapsed = time.monotonic() - began
        finally:
            tick.CLI_FALLBACK = original
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 12.0)
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        self.assertEqual(receipt["status"], "cli_fallback_expired")

    def test_fallback_expired_budget_launches_no_child(self):
        tick = self.tick
        marker = Path(self._tmp.name) / "child-ran"
        child = self._write_child(f'touch "{marker}"\n')
        original = tick.CLI_FALLBACK
        tick.CLI_FALLBACK = str(child)
        try:
            deadline = tick.TickDeadline(self.clock.value)
            self.clock.value += 779.0  # 1s remain
            code = tick.run_cli_fallback(_args(tick), PROMPT, deadline)
        finally:
            tick.CLI_FALLBACK = original
        self.assertEqual(code, 1)
        self.assertFalse(marker.exists())
        receipt = json.loads(
            (self.state / "scheduler-wakeup-latest.json").read_text())
        self.assertEqual(receipt["status"], "cli_fallback_budget_exhausted")


# --- acceptance 10: receipt privacy and durability ---------------------------------

class ReceiptPrivacyTests(TickTestCase):
    async def test_receipts_are_private_bounded_and_canary_free(self):
        fake = FakeCodex(self.tick, events_after_start=[
            {"method": "thread/updated",
             "params": {"note": "CANARY-EVENT-SECRET-xyz",
                        "token": "CANARY-TOKEN-abc"}}])
        await self.run_tick(fake)
        path = self.state / "scheduler-wakeup-latest.json"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        blob = path.read_text()
        self.assertLessEqual(len(blob.encode()), 65536)
        for canary in ("CANARY-PROMPT-SECRET-abc123", "CANARY-EVENT-SECRET-xyz",
                       "CANARY-TOKEN-abc"):
            self.assertNotIn(canary, blob)
        receipt = json.loads(blob)
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual(receipt["turn_id"], OWNED_TURN)


class SendDeadlineTests(TickTestCase):
    async def test_ambiguous_start_send_is_bounded_and_never_retried(self):
        from websockets.asyncio.client import ClientConnection

        self.tick._now = time.monotonic
        self.tick._sleep = asyncio.sleep
        fake = FakeCodex(self.tick)
        original_send = ClientConnection.send
        attempts = []

        async def blocked_send(ws, message, *args, **kwargs):
            request = json.loads(message)
            await original_send(ws, message, *args, **kwargs)
            if request.get("method") == "turn/start":
                attempts.append(request["params"]["clientUserMessageId"])
                await asyncio.Future()

        async with websockets.unix_serve(fake.serve, str(self.socket_path)):
            deadline = self.tick.TickDeadline(time.monotonic(), total=0.25)
            ws, rpc = await self.tick.open_connection(deadline)
            receipt = {"client_message_id": NONCE, "status": "turn_start_intent"}
            try:
                with mock.patch.object(ClientConnection, "send", blocked_send):
                    code = await self.tick._attempt_and_supervise(
                        _args(self.tick), PROMPT, receipt, ws, rpc, deadline, NONCE, "paused")
            finally:
                ws.transport.abort()
        self.assertEqual(code, 1)
        self.assertEqual(attempts, [NONCE])
        self.assertEqual(len(fake.starts), 1)
        self.assertFalse(fake.interrupts)
        self.assertEqual(receipt["client_message_id"], NONCE)
        self.assertEqual(receipt["status"], "ownership_unresolved_at_deadline")
        self.assertLess(time.monotonic() - deadline.started, 2.0)

    async def test_initialized_send_is_bounded_before_any_start(self):
        from websockets.asyncio.client import ClientConnection

        self.tick._now = time.monotonic
        fake = FakeCodex(self.tick)
        original_send = ClientConnection.send
        initialized_attempts = []

        async def blocked_send(ws, message, *args, **kwargs):
            if json.loads(message).get("method") == "initialized":
                initialized_attempts.append(True)
                await asyncio.Future()
            await original_send(ws, message, *args, **kwargs)

        async with websockets.unix_serve(fake.serve, str(self.socket_path)):
            deadline = self.tick.TickDeadline(time.monotonic(), total=0.25)
            with mock.patch.object(ClientConnection, "send", blocked_send):
                with self.assertRaises(asyncio.TimeoutError):
                    await self.tick.open_connection(deadline)
        self.assertEqual(initialized_attempts, [True])
        self.assertFalse(fake.starts)
        self.assertLess(time.monotonic() - deadline.started, 2.0)

    async def test_reconnect_cleanup_uses_remaining_deadline(self):
        self.tick._now = time.monotonic
        transport = types.SimpleNamespace(abort=mock.Mock())

        async def blocked_close():
            await asyncio.Future()

        ws = types.SimpleNamespace(close=blocked_close, transport=transport)
        deadline = self.tick.TickDeadline(time.monotonic(), total=0.05)
        await self.tick.abandon_connection(ws, deadline)
        transport.abort.assert_called_once_with()
        self.assertLess(time.monotonic() - deadline.started, 0.75)


# --- foreground flock lifecycle (subprocess) ---------------------------------------

HARNESS = r'''
import asyncio
import fcntl
import importlib.util
import sys
import types
from pathlib import Path

import websockets

sys.path.insert(0, sys.argv[2])
import test_concierge_chat_tick as harness  # noqa: E402

module_path, tests_dir, lock_path, workdir, phase_dir = sys.argv[1:6]
interrupt_mode = len(sys.argv) > 6 and sys.argv[6] == "interrupt"
spec = importlib.util.spec_from_file_location("tick", module_path)
tick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tick)

clock = harness.FakeClock()
state = Path(workdir) / "state"
state.mkdir(parents=True, exist_ok=True)
(state / "mandate.json").write_text("{}")
socket_path = Path(workdir) / "server.sock"
tick.STATE = state
tick.SOCKET = socket_path
tick._now = clock.now
# Real (not fake) sleep: the visibility/ownership phases must span observable
# wall-clock time so the foreground-lock assertions are meaningful.
async def paced_sleep(seconds):
    if interrupt_mode:
        clock.value += max(0.0, seconds)
        await asyncio.sleep(0.1 if (Path(phase_dir) / "interrupt-ack").exists() else 0)
    else:
        await asyncio.sleep(seconds)

tick._sleep = paced_sleep
tick._new_nonce = lambda: harness.NONCE

phases = Path(phase_dir)
original_store = tick.store

def recording(value, filename="scheduler-wakeup-latest.json"):
    original_store(value, filename)
    if value.get("status") == "turn_start_intent":
        (phases / "intent").write_text("")
    if value.get("status") == "running":
        (phases / "owned").write_text("")
    if value.get("status") == "owned_turn_termination_pending" and \
            value.get("interrupt_request_state") == "acknowledged":
        (phases / "interrupt-ack").write_text("")

tick.store = recording

fake = harness.FakeCodex(tick, items_delayed_until=3)
fake.turn_status_fn = lambda: "completed" \
    if not interrupt_mode and fake.items_calls.get(harness.OWNED_TURN, 0) >= 3 \
    else "inProgress"

async def main():
    lock = open(lock_path, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    (phases / "locked").write_text("")
    args = types.SimpleNamespace(model="gpt-6.1-sol", effort="xhigh",
                                 inspect_only=False, wakeup_id="harness")
    async with websockets.unix_serve(fake.serve, str(socket_path)):
        code = await tick.observe_or_run(args, "harness prompt", clock.value)
    (phases / "done").write_text(str(code))

asyncio.run(main())
'''


def _lock_available(path: Path) -> bool:
    fd = os.open(str(path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class FlockLifecycleTests(unittest.TestCase):
    def test_foreground_lock_held_through_visibility_and_ownership(self):
        self._assert_lock_lifetime(interrupt_mode=False)

    def test_foreground_lock_held_after_interrupt_acknowledgment(self):
        self._assert_lock_lifetime(interrupt_mode=True)

    def _assert_lock_lifetime(self, interrupt_mode):
        tmp = tempfile.TemporaryDirectory()
        try:
            tmp_path = Path(tmp.name)
            phases = tmp_path / "phases"
            phases.mkdir()
            lock_path = tmp_path / "tick.lock"
            lock_path.touch()
            harness = tmp_path / "harness.py"
            harness.write_text(HARNESS)
            proc = subprocess.Popen(
                [sys.executable, str(harness), str(MODULE_PATH),
                 str(Path(__file__).parent), str(lock_path),
                 str(tmp_path), str(phases)] + (["interrupt"] if interrupt_mode else []),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            try:
                def wait_for_phase(name: str) -> None:
                    deadline_at = time.monotonic() + 30.0
                    while not (phases / name).exists():
                        if time.monotonic() > deadline_at:
                            out, err = proc.communicate(timeout=5.0)
                            raise AssertionError(
                                f"harness never reached phase {name!r}\n"
                                f"stdout={out!r}\nstderr={err!r}")
                        time.sleep(0.02)

                wait_for_phase("locked")
                self.assertFalse(_lock_available(lock_path),
                                 "lock must be held by the tick")
                wait_for_phase("intent")
                self.assertFalse(_lock_available(lock_path),
                                 "lock must stay held while ownership "
                                 "visibility is pending")
                wait_for_phase("owned")
                self.assertFalse(_lock_available(lock_path),
                                 "lock must stay held during proven-owned "
                                 "execution")
                if interrupt_mode:
                    wait_for_phase("interrupt-ack")
                    self.assertFalse(_lock_available(lock_path),
                                     "acknowledged interruption must retain the lock")
                out, err = proc.communicate(timeout=30.0)
                self.assertEqual((phases / "done").read_text(), "1" if interrupt_mode else "0")
                self.assertFalse(out.strip(), out)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            self.assertTrue(_lock_available(lock_path),
                            "lock must be released after completion")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
