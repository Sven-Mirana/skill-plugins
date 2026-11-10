#!/usr/bin/env python3
"""Negative-first tests for the bounded V5 agent wake-intent core."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_wakeup
import collaboration_panel


ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "fixtures" / "collaboration-panel" / "wakeup-policies.json"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)

    def future(self, seconds: int) -> str:
        return (self.value + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class WakeupCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="sublation-wakeup-test-"))
        self.panel = collaboration_panel.PanelStore(self.temp)
        self.panel.initialize(project_name="wakeup-test", user_name="root")
        self.clock = Clock()
        self.store = agent_wakeup.WakeupStore(self.temp, clock=self.clock)
        self.fixture_policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.policy = copy.deepcopy(self.fixture_policy)
        artifact_sha256 = "sha256:" + hashlib.sha256(
            (ROOT / "scripts" / "agent_wakeup.py").read_bytes()
        ).hexdigest()
        self.policy["registered_adapters"] = [
            {
                "adapter_id": "claude-code-watcher-auto-v1",
                "adapter_sha256": artifact_sha256,
                "kind": "watcher_auto",
                "allowed_seat_ids": ["claude-code"],
            },
            {
                "adapter_id": "hermes-auto-wakeup-v1",
                "adapter_sha256": artifact_sha256,
                "kind": "local_wakeup",
                "allowed_seat_ids": ["hermes"],
            },
        ]
        self.policy["pending_host_installs"] = []
        for item in self.policy["seat_policies"]:
            item["enabled"] = True
            item["mode"] = "intent_only"
            item["adapter_id"] = (
                "claude-code-watcher-auto-v1"
                if item["seat_id"] == "claude-code"
                else "hermes-auto-wakeup-v1"
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def initialize(self, policy: dict | None = None) -> dict:
        return self.store.initialize(policy or self.policy)

    def arm(self, *, quiet_cycles: int = 20, max_wakes: int = 20, seconds: int = 3500) -> dict:
        return self.store.arm_window(
            deadline=self.clock.future(seconds),
            quiet_cycles=quiet_cycles,
            max_wakes=max_wakes,
        )

    def send(self, *, speaker: str = "user", audience: str = "claude-code", body: str = "请复核") -> dict:
        return self.panel.send_message(speaker=speaker, audience=audience, body=body)

    def claim(self, intent: dict) -> dict:
        receipt_key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        return self.store.claim(
            intent_id=intent["intent_id"],
            adapter_id=intent["adapter_id"],
            adapter_sha256=intent["adapter_sha256"],
            claim_proof_hmac=agent_wakeup.build_claim_proof(intent, receipt_key),
        )

    def complete(self, claim: dict, *, status: str, suffix: str = "1") -> dict:
        receipt = agent_wakeup.build_receipt(
            claim,
            receipt_key=agent_wakeup.load_host_receipt_key(self.store.receipt_key_path),
            status=status,
            delivery_ref=f"local-delivery-{suffix}" if status == "sent_and_verified" else None,
            failure_code=f"adapter_failed_{suffix}" if status == "failed" else None,
            now=self.clock(),
        )
        return self.store.complete(receipt)

    def wake_hashes(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.store.state_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.store.state_dir.rglob("*"))
            if path.is_file()
        }

    def pause_retirement_before_commit(
        self,
        *,
        bypass_wakeup_guard: bool,
    ) -> tuple[threading.Event, threading.Event, list[BaseException], threading.Thread, mock._patch]:
        reached_commit = threading.Event()
        allow_commit = threading.Event()
        failures: list[BaseException] = []
        original_commit = self.panel._commit_roster

        def delayed_commit(**kwargs: object) -> None:
            reached_commit.set()
            if not allow_commit.wait(5):
                raise RuntimeError("test retirement barrier timed out")
            original_commit(**kwargs)

        patches = [mock.patch.object(self.panel, "_commit_roster", side_effect=delayed_commit)]
        if bypass_wakeup_guard:
            patches.append(
                mock.patch.object(
                    self.panel,
                    "_guard_wakeup_retirement_unlocked",
                    return_value=None,
                )
            )
        for patcher in patches:
            patcher.start()

        def retire() -> None:
            try:
                self.panel.disable_agent(seat_id="claude-code", expected_revision=1)
            except BaseException as exc:  # captured for deterministic thread assertion
                failures.append(exc)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

        thread = threading.Thread(target=retire, name="retire-claude-code", daemon=True)
        thread.start()
        return reached_commit, allow_commit, failures, thread, patches[0]

    def test_default_closed_and_dynamic_seat_is_disabled_queue_only_with_baseline(self) -> None:
        snapshot = self.initialize()
        self.assertEqual(snapshot["wake_window"]["state"], "closed")
        self.assertTrue(snapshot["policy"]["requires_explicit_user_arm"])
        before = self.send(body="动态席加入前")
        revision = self.panel.snapshot()["roster"]["revision"]
        self.panel.add_agent(
            seat_id="specialist-a",
            display_name="专项 Agent",
            expected_revision=revision,
        )
        snapshot = self.store.snapshot()
        row = next(item for item in snapshot["seats"] if item["seat_id"] == "specialist-a")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["mode"], "queue_only")
        self.assertEqual(row["baseline_state"], "not_baselined")
        self.assertIsNone(row["cursor_message_id"])
        self.assertIsNone(row["adapter_id"])
        self.arm()
        self.send(audience="specialist-a", body="动态席加入后")
        result = self.store.discover("specialist-a")
        self.assertIsNone(result["intent"])
        self.assertEqual(result["reason"], "idle_silent")
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "specialist-a")
        self.assertEqual(row["baseline_state"], "baselined")
        self.assertNotEqual(row["cursor_message_id"], before["id"])

    def test_panel_snapshot_is_byte_for_byte_read_only_for_unsynchronised_seat(self) -> None:
        self.initialize()
        revision = self.panel.snapshot()["roster"]["revision"]
        self.panel.add_agent(
            seat_id="observer-new",
            display_name="新观察席",
            expected_revision=revision,
        )

        def hashes() -> dict[str, str]:
            return {
                str(path.relative_to(self.store.state_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.store.state_dir.rglob("*"))
                if path.is_file()
            }

        before = hashes()
        snapshot = self.store.panel_snapshot()
        after = hashes()
        self.assertEqual(after, before)
        row = next(item for item in snapshot["seats"] if item["seat_id"] == "observer-new")
        self.assertEqual(row["baseline_state"], "not_baselined")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["mode"], "queue_only")

    def test_requires_explicit_user_arm_and_window_deadline_is_bounded(self) -> None:
        self.initialize()
        self.send()
        self.assertEqual(self.store.discover()["reason"], "window_closed")
        with self.assertRaisesRegex(agent_wakeup.GateBlocked, "only the user"):
            self.store.arm_window(
                deadline=self.clock.future(60),
                quiet_cycles=1,
                max_wakes=1,
                actor="claude-code",
            )
        with self.assertRaisesRegex(agent_wakeup.GateBlocked, "deadline"):
            self.store.arm_window(
                deadline=self.clock.future(3601),
                quiet_cycles=1,
                max_wakes=1,
            )

    def test_retire_restore_stays_suspended_until_new_user_window_rearms(self) -> None:
        self.initialize()
        self.arm()
        revision = self.panel.snapshot()["roster"]["revision"]
        self.panel.disable_agent(
            seat_id="claude-code",
            expected_revision=revision,
            reason="测试停用",
        )
        revision = self.panel.snapshot()["roster"]["revision"]
        self.panel.restore_agent(
            seat_id="claude-code",
            expected_revision=revision,
            reason="测试恢复",
        )
        during_suspend = self.send(body="恢复后、重新 arm 前不得自动唤醒")
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertTrue(row["enabled"])
        self.assertFalse(row["effective_enabled"])
        self.assertTrue(row["requires_rearm"])
        blocked = self.store.discover("claude-code")
        self.assertIn("requires_rearm", blocked["reason"])
        self.store.drain_window(reason="测试换窗")
        self.store.close_window()
        self.arm()
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertTrue(row["effective_enabled"])
        self.assertFalse(row["requires_rearm"])
        self.assertEqual(row["cursor_message_id"], during_suspend["id"])
        fresh = self.send(body="重新 arm 后的新消息")
        intent = self.store.discover("claude-code")["intent"]
        self.assertEqual(intent["source_message_id"], fresh["id"])

    def test_self_trigger_and_exact_duplicate_are_deduplicated(self) -> None:
        self.initialize()
        self.arm()
        self.send(speaker="claude-code", audience="all", body="自己的输出不得唤醒自己")
        target = self.send(body="唯一有效触发")
        with self.store.messages_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(target, ensure_ascii=False, sort_keys=True) + "\n")
        result = self.store.discover("claude-code")
        self.assertEqual(result["intent"]["source_message_id"], target["id"])
        claimed = self.claim(result["intent"])
        self.complete(claimed, status="sent_and_verified")
        idle = self.store.discover("claude-code")
        self.assertIsNone(idle["intent"])
        self.assertEqual(idle["reason"], "idle_silent")

    def test_unauthorized_adapter_and_fingerprint_are_rejected(self) -> None:
        self.initialize()
        self.arm()
        self.send()
        intent = self.store.discover("claude-code")["intent"]
        proof = agent_wakeup.build_claim_proof(
            intent,
            agent_wakeup.load_host_receipt_key(self.store.receipt_key_path),
        )
        with self.assertRaises(agent_wakeup.GateBlocked) as unknown:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id="unregistered-adapter",
                adapter_sha256=intent["adapter_sha256"],
                claim_proof_hmac=proof,
            )
        self.assertEqual(unknown.exception.code, "adapter_unauthorized")
        with self.assertRaises(agent_wakeup.GateBlocked) as drifted:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id=intent["adapter_id"],
                adapter_sha256="sha256:" + "0" * 64,
                claim_proof_hmac=proof,
            )
        self.assertEqual(drifted.exception.code, "adapter_unauthorized")

    def test_cross_project_or_non_loopback_binding_fails_closed(self) -> None:
        self.initialize()
        config = json.loads(self.store.panel_config_path.read_text(encoding="utf-8"))
        config["project_root"] = str(self.temp / "other")
        self.store.panel_config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(agent_wakeup.ProjectBindingError):
            self.store.snapshot()

    def test_lock_cooldown_and_hourly_quota_gate(self) -> None:
        policy = copy.deepcopy(self.policy)
        for item in policy["seat_policies"]:
            if item["seat_id"] == "claude-code":
                item["hourly_limit"] = 2
        self.initialize(policy)
        self.arm()
        descriptor = os.open(self.store.lock_path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(agent_wakeup.GateBlocked) as busy:
                self.store.snapshot()
            self.assertEqual(busy.exception.code, "lock_busy")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.send(body="一")
        first = self.store.discover("claude-code")["intent"]
        self.complete(self.claim(first), status="sent_and_verified", suffix="1")
        self.send(body="二")
        cooldown = self.store.discover("claude-code")
        self.assertIn("cooldown", cooldown["reason"])
        self.clock.advance(60)
        second = self.store.discover("claude-code")["intent"]
        self.complete(self.claim(second), status="sent_and_verified", suffix="2")
        self.send(body="三")
        self.clock.advance(60)
        quota = self.store.discover("claude-code")
        self.assertIn("hourly_quota", quota["reason"])

    def test_failed_receipt_does_not_advance_and_retry_backoff_ends_terminal_hold(self) -> None:
        self.initialize()
        self.arm()
        source = self.send(body="需要有限重试")
        panel_cursor_before = self.panel._read_cursor("claude-code")["last_seen_id"]
        expected_delays = [60, 180, 600]
        idempotency_key = None
        for attempt in range(1, 5):
            intent = self.store.discover("claude-code")["intent"]
            self.assertEqual(intent["attempt"], attempt)
            idempotency_key = idempotency_key or intent["idempotency_key"]
            self.assertEqual(intent["idempotency_key"], idempotency_key)
            result = self.complete(self.claim(intent), status="failed", suffix=str(attempt))
            snapshot = self.store.snapshot()
            row = next(item for item in snapshot["seats"] if item["seat_id"] == "claude-code")
            self.assertNotEqual(row["cursor_message_id"], source["id"])
            self.assertEqual(
                self.panel._read_cursor("claude-code")["last_seen_id"],
                panel_cursor_before,
            )
            if attempt < 4:
                blocked = self.store.discover("claude-code")
                self.assertIn(f"retry_backoff_{expected_delays[attempt - 1]}s", blocked["reason"])
                self.clock.advance(expected_delays[attempt - 1])
            else:
                self.assertTrue(result["terminal_hold"])
                self.assertEqual(result["outcome"], "terminal_hold")
        held = self.store.discover("claude-code")
        self.assertIn("terminal_hold", held["reason"])
        states = self.store._intent_states()
        self.assertEqual(sum(item["idempotency_key"] == idempotency_key for item in states.values()), 4)
        self.assertEqual(list(states.values())[-1]["status"], "terminal_hold")

    def test_only_sent_and_verified_advances_cursor(self) -> None:
        self.initialize()
        self.arm()
        source = self.send()
        intent = self.store.discover("claude-code")["intent"]
        result = self.complete(self.claim(intent), status="sent_and_verified")
        self.assertTrue(result["cursor_advanced"])
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertEqual(row["cursor_message_id"], source["id"])
        self.assertEqual(self.panel.inbox("claude-code")["pending_count"], 0)

    def test_claim_and_receipt_require_hmac_nonce_and_reject_replay_without_mutation(self) -> None:
        self.initialize()
        self.assertEqual(stat.S_IMODE(self.store.receipt_key_path.stat().st_mode), 0o600)
        self.arm()
        source = self.send(body="HMAC 保护")
        intent = self.store.discover("claude-code")["intent"]

        def log_hashes() -> tuple[str, str]:
            return (
                hashlib.sha256(self.store.events_path.read_bytes()).hexdigest(),
                hashlib.sha256(self.store.intents_path.read_bytes()).hexdigest(),
            )

        before = log_hashes()
        with self.assertRaises(agent_wakeup.GateBlocked) as absent:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id=intent["adapter_id"],
                adapter_sha256=intent["adapter_sha256"],
            )
        self.assertEqual(absent.exception.code, "claim_proof_invalid")
        self.assertEqual(log_hashes(), before)
        with self.assertRaises(agent_wakeup.GateBlocked) as wrong:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id=intent["adapter_id"],
                adapter_sha256=intent["adapter_sha256"],
                claim_proof_hmac="hmac-sha256:" + "0" * 64,
            )
        self.assertEqual(wrong.exception.code, "claim_proof_invalid")
        self.assertEqual(log_hashes(), before)

        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        claim = self.store.claim(
            intent_id=intent["intent_id"],
            adapter_id=intent["adapter_id"],
            adapter_sha256=intent["adapter_sha256"],
            claim_proof_hmac=agent_wakeup.build_claim_proof(intent, key),
        )
        valid = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="sent_and_verified",
            delivery_ref="verified-local-delivery",
            now=self.clock(),
        )

        forged = copy.deepcopy(valid)
        forged["receipt_hmac"] = "hmac-sha256:" + "0" * 64
        with self.assertRaises(agent_wakeup.WakeupError) as self_hash_only:
            self.store.complete(forged)
        self.assertEqual(self_hash_only.exception.code, "receipt_hmac_invalid")

        wrong_key = agent_wakeup.build_receipt(
            claim,
            receipt_key=b"x" * 32,
            status="sent_and_verified",
            delivery_ref="verified-local-delivery",
            now=self.clock(),
        )
        with self.assertRaises(agent_wakeup.WakeupError) as bad_key:
            self.store.complete(wrong_key)
        self.assertEqual(bad_key.exception.code, "receipt_hmac_invalid")

        wrong_nonce = copy.deepcopy(valid)
        wrong_nonce["nonce"] = "f" * 64
        wrong_nonce["receipt_sha256"] = agent_wakeup.receipt_sha256(wrong_nonce)
        wrong_nonce["receipt_hmac"] = agent_wakeup._receipt_hmac(wrong_nonce, key)
        with self.assertRaises(agent_wakeup.ProjectBindingError):
            self.store.complete(wrong_nonce)

        accepted = self.store.complete(valid)
        self.assertTrue(accepted["cursor_advanced"])
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertEqual(row["cursor_message_id"], source["id"])
        replay = self.store.complete(valid)
        self.assertTrue(replay["idempotent_replay"])

    def test_retirement_guard_to_commit_gap_blocks_concurrent_discover(self) -> None:
        self.initialize()
        self.arm()
        message = self.send(body="退休竞态消息")
        self.panel.acknowledge(seat_id="claude-code", message_id=message["id"])
        before = self.wake_hashes()
        original_guard = self.panel._guard_wakeup_retirement_unlocked
        guard_passed = threading.Event()
        allow_guard_return = threading.Event()
        failures: list[BaseException] = []

        def paused_guard(*, roster: dict, seat_id: str) -> None:
            original_guard(roster=roster, seat_id=seat_id)
            guard_passed.set()
            if not allow_guard_return.wait(5):
                raise RuntimeError("test guard barrier timed out")

        def retire() -> None:
            try:
                self.panel.disable_agent(seat_id="claude-code", expected_revision=1)
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
            self.panel,
            "_guard_wakeup_retirement_unlocked",
            side_effect=paused_guard,
        ):
            thread = threading.Thread(target=retire, daemon=True)
            thread.start()
            self.assertTrue(guard_passed.wait(5))
            with self.assertRaises(agent_wakeup.GateBlocked) as racing:
                self.store.discover("claude-code")
            self.assertEqual(racing.exception.code, "panel_lock_busy")
            self.assertEqual(self.wake_hashes(), before)
            allow_guard_return.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        with self.assertRaises(agent_wakeup.GateBlocked) as retired:
            self.store.discover("claude-code")
        self.assertEqual(retired.exception.code, "seat_unavailable")
        self.assertEqual(self.wake_hashes(), before)
        self.assertEqual(self.store._intent_states(), {})
        self.assertTrue(self.store.validate()["ok"])

    def test_forced_retirement_race_blocks_claim_before_and_after_commit(self) -> None:
        self.initialize()
        self.arm()
        message = self.send(body="claim 竞态")
        self.panel.acknowledge(seat_id="claude-code", message_id=message["id"])
        intent = self.store.discover("claude-code")["intent"]
        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        proof = agent_wakeup.build_claim_proof(intent, key)
        reached, release, failures, thread, _ = self.pause_retirement_before_commit(
            bypass_wakeup_guard=True
        )
        self.assertTrue(reached.wait(5))
        before = self.wake_hashes()
        with self.assertRaises(agent_wakeup.GateBlocked) as racing:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id=intent["adapter_id"],
                adapter_sha256=intent["adapter_sha256"],
                claim_proof_hmac=proof,
            )
        self.assertEqual(racing.exception.code, "panel_lock_busy")
        self.assertEqual(self.wake_hashes(), before)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        with self.assertRaises(agent_wakeup.GateBlocked) as retired:
            self.store.claim(
                intent_id=intent["intent_id"],
                adapter_id=intent["adapter_id"],
                adapter_sha256=intent["adapter_sha256"],
                claim_proof_hmac=proof,
            )
        self.assertEqual(retired.exception.code, "seat_inactive")
        self.assertEqual(self.wake_hashes(), before)
        self.assertEqual(self.store._intent_states()[intent["intent_id"]]["status"], "pending")
        self.assertTrue(self.store.validate()["ok"])

    def test_forced_retirement_race_blocks_complete_and_preserves_cursor(self) -> None:
        self.initialize()
        self.arm()
        message = self.send(body="complete 竞态")
        self.panel.acknowledge(seat_id="claude-code", message_id=message["id"])
        intent = self.store.discover("claude-code")["intent"]
        claim = self.claim(intent)
        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        receipt = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="sent_and_verified",
            delivery_ref="retirement-race",
            now=self.clock(),
        )
        cursor_before = json.loads(self.store.cursors_path.read_text(encoding="utf-8"))
        reached, release, failures, thread, _ = self.pause_retirement_before_commit(
            bypass_wakeup_guard=True
        )
        self.assertTrue(reached.wait(5))
        before = self.wake_hashes()
        with self.assertRaises(agent_wakeup.GateBlocked) as racing:
            self.store.complete(receipt)
        self.assertEqual(racing.exception.code, "panel_lock_busy")
        self.assertEqual(self.wake_hashes(), before)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        with self.assertRaises(agent_wakeup.GateBlocked) as retired:
            self.store.complete(receipt)
        self.assertEqual(retired.exception.code, "seat_inactive")
        self.assertEqual(self.wake_hashes(), before)
        self.assertEqual(
            json.loads(self.store.cursors_path.read_text(encoding="utf-8")),
            cursor_before,
        )
        self.assertEqual(self.store._intent_states()[intent["intent_id"]]["status"], "claimed")
        self.assertTrue(self.store.validate()["ok"])

    def test_expired_receipt_times_out_atomically_and_invalid_time_order_is_rejected(self) -> None:
        self.initialize()
        self.arm()
        source = self.send(body="过期 receipt")
        claim = self.claim(self.store.discover("claude-code")["intent"])
        cursor_before = json.loads(self.store.cursors_path.read_text(encoding="utf-8"))
        self.clock.advance(181)
        late = agent_wakeup.build_receipt(
            claim,
            receipt_key=agent_wakeup.load_host_receipt_key(self.store.receipt_key_path),
            status="sent_and_verified",
            delivery_ref="late",
            now=self.clock(),
        )
        with self.assertRaises(agent_wakeup.GateBlocked) as expired:
            self.store.complete(late)
        self.assertEqual(expired.exception.code, "intent_expired")
        self.assertEqual(
            self.store._intent_states()[claim["intent_id"]]["status"],
            "root_hold",
        )
        self.assertEqual(json.loads(self.store.cursors_path.read_text(encoding="utf-8")), cursor_before)
        self.assertNotEqual(
            next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")["cursor_message_id"],
            source["id"],
        )
        self.assertTrue(self.store.validate()["ok"])

    def test_receipt_time_and_window_generation_bindings_are_exact(self) -> None:
        self.initialize()
        self.arm()
        self.send(body="时间和窗口绑定")
        intent = self.store.discover("claude-code")["intent"]
        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        claim = self.claim(intent)
        valid = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="sent_and_verified",
            delivery_ref="binding-test",
            now=self.clock(),
        )
        before = self.wake_hashes()

        reversed_time = copy.deepcopy(valid)
        reversed_time["sent_at"] = self.clock.future(2)
        reversed_time["verified_at"] = self.clock.future(1)
        reversed_time["receipt_sha256"] = agent_wakeup.receipt_sha256(reversed_time)
        reversed_time["receipt_hmac"] = agent_wakeup._receipt_hmac(reversed_time, key)
        with self.assertRaises(agent_wakeup.WakeupError) as ordering:
            self.store.complete(reversed_time)
        self.assertEqual(ordering.exception.code, "receipt_time_invalid")

        future = copy.deepcopy(valid)
        future["verified_at"] = self.clock.future(
            agent_wakeup.MAX_RECEIPT_CLOCK_SKEW_SECONDS + 1
        )
        future["receipt_sha256"] = agent_wakeup.receipt_sha256(future)
        future["receipt_hmac"] = agent_wakeup._receipt_hmac(future, key)
        with self.assertRaises(agent_wakeup.WakeupError) as future_error:
            self.store.complete(future)
        self.assertEqual(future_error.exception.code, "receipt_time_invalid")

        before_claim = copy.deepcopy(valid)
        claimed_at = agent_wakeup._parse_time(claim["claimed_at"], "claimed_at")
        too_early = claimed_at - timedelta(
            seconds=agent_wakeup.MAX_RECEIPT_CLOCK_SKEW_SECONDS + 1
        )
        before_claim["sent_at"] = agent_wakeup._iso(too_early)
        before_claim["verified_at"] = agent_wakeup._iso(too_early)
        before_claim["receipt_sha256"] = agent_wakeup.receipt_sha256(before_claim)
        before_claim["receipt_hmac"] = agent_wakeup._receipt_hmac(before_claim, key)
        with self.assertRaises(agent_wakeup.WakeupError) as predates_claim:
            self.store.complete(before_claim)
        self.assertEqual(predates_claim.exception.code, "receipt_time_invalid")

        for field, replacement in (
            ("window_id", "wake-window-forged"),
            ("seat_generation", "sha256:" + "f" * 64),
            ("policy_sha256", "sha256:" + "e" * 64),
        ):
            forged = copy.deepcopy(valid)
            forged[field] = replacement
            forged["receipt_sha256"] = agent_wakeup.receipt_sha256(forged)
            forged["receipt_hmac"] = agent_wakeup._receipt_hmac(forged, key)
            with self.subTest(field=field):
                with self.assertRaises(agent_wakeup.ProjectBindingError):
                    self.store.complete(forged)
        self.assertEqual(self.wake_hashes(), before)

    def test_timeout_escalates_to_root_without_advancing_or_same_cycle_retry(self) -> None:
        self.initialize()
        self.arm()
        source = self.send()
        intent = self.store.discover("claude-code")["intent"]
        self.claim(intent)
        self.clock.advance(181)
        result = self.store.discover("claude-code")
        self.assertEqual(result["reason"], "timeout_escalated_to_root")
        self.assertEqual(result["escalated_intent_ids"], [intent["intent_id"]])
        self.assertIsNone(result["intent"])
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertNotEqual(row["cursor_message_id"], source["id"])
        self.assertEqual(self.store._intent_states()[intent["intent_id"]]["status"], "root_hold")
        intent_records_before = len(self.store._verify_chain(self.store.intents_path, "record_hash"))
        held = self.store.discover("claude-code")
        self.assertIn("root_hold", held["reason"])
        self.assertIsNone(held["intent"])
        self.assertEqual(
            len(self.store._verify_chain(self.store.intents_path, "record_hash")),
            intent_records_before,
        )
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertTrue(row["awaiting_user_disposition"])
        with self.assertRaises(agent_wakeup.GateBlocked) as non_user:
            self.store.dispose_hold(
                intent_id=intent["intent_id"],
                source_message_id=source["id"],
                window_id=intent["window_id"],
                reason="非用户不得裁决",
                actor="claude-code",
            )
        self.assertEqual(non_user.exception.code, "user_action_required")
        with self.assertRaises(agent_wakeup.ProjectBindingError):
            self.store.dispose_hold(
                intent_id=intent["intent_id"],
                source_message_id="msg-forged-source",
                window_id=intent["window_id"],
                reason="绑定不匹配",
            )
        decision = self.store.dispose_hold(
            intent_id=intent["intent_id"],
            source_message_id=source["id"],
            window_id=intent["window_id"],
            reason="用户批准单次重试",
        )
        self.assertEqual(decision["disposition"], "retry_once")
        retried = self.store.discover("claude-code")["intent"]
        self.assertEqual(retried["source_message_id"], source["id"])
        self.assertEqual(retried["attempt"], 2)

    def test_initialization_publish_blocks_get_without_half_state_and_is_retryable(self) -> None:
        publish_reached = threading.Event()
        allow_publish = threading.Event()
        init_failures: list[BaseException] = []
        snapshot_results: list[dict] = []
        original_publish = self.store._publish_staging

        def delayed_publish(staging: Path) -> None:
            publish_reached.set()
            if not allow_publish.wait(5):
                raise RuntimeError("publish barrier timed out")
            original_publish(staging)

        def initialize() -> None:
            try:
                self.store.initialize(self.policy)
            except BaseException as exc:
                init_failures.append(exc)

        def get_snapshot() -> None:
            snapshot_results.append(self.panel.snapshot())

        with mock.patch.object(self.store, "_publish_staging", side_effect=delayed_publish):
            init_thread = threading.Thread(target=initialize, daemon=True)
            init_thread.start()
            self.assertTrue(publish_reached.wait(5))
            self.assertFalse(self.store._final_state_dir.exists())
            get_thread = threading.Thread(target=get_snapshot, daemon=True)
            get_thread.start()
            self.assertTrue(get_thread.is_alive())
            allow_publish.set()
            init_thread.join(5)
            get_thread.join(5)
        self.assertEqual(init_failures, [])
        self.assertEqual(snapshot_results[0]["wakeup"]["status"], "ready")
        before = self.wake_hashes()
        self.panel.snapshot()
        self.assertEqual(self.wake_hashes(), before)

        retry_root = Path(tempfile.mkdtemp(prefix="sublation-wakeup-retry-"))
        try:
            retry_panel = collaboration_panel.PanelStore(retry_root)
            retry_panel.initialize(project_name="retry", user_name="root")
            retry_store = agent_wakeup.WakeupStore(retry_root, clock=self.clock)
            with mock.patch.object(
                retry_store,
                "_publish_staging",
                side_effect=RuntimeError("simulated publish failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "publish failure"):
                    retry_store.initialize(self.policy)
            self.assertFalse(retry_store._final_state_dir.exists())
            self.assertEqual(retry_panel.snapshot()["wakeup"]["status"], "default_off")
            self.assertFalse(retry_store._final_state_dir.exists())
            self.assertEqual(retry_store.initialize(self.policy)["wake_window"]["state"], "closed")
        finally:
            shutil.rmtree(retry_root)

    def test_first_arm_rebaselines_retire_restore_generation(self) -> None:
        self.initialize()
        self.panel.disable_agent(seat_id="claude-code", expected_revision=1)
        retired_message = self.send(audience="all", body="退休期间广播")
        self.panel.restore_agent(seat_id="claude-code", expected_revision=2)
        self.arm()
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertEqual(row["cursor_message_id"], retired_message["id"])
        idle = self.store.discover("claude-code")
        self.assertIsNone(idle["intent"])
        fresh = self.send(body="首窗仅接收恢复后的新消息")
        self.assertEqual(
            self.store.discover("claude-code")["intent"]["source_message_id"],
            fresh["id"],
        )

    def test_policy_digest_drift_fails_closed_and_default_fixture_stays_unregistered(self) -> None:
        self.initialize()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["policy"]["revision"], 1)
        self.assertEqual(snapshot["policy"]["sha256"], agent_wakeup._sha256(self.policy))
        drifted = json.loads(self.store.policy_path.read_text(encoding="utf-8"))
        drifted["seat_policies"][0]["cooldown_seconds"] += 1
        self.store.policy_path.write_text(json.dumps(drifted), encoding="utf-8")
        with self.assertRaises(agent_wakeup.GateBlocked) as drift:
            self.store.snapshot()
        self.assertEqual(drift.exception.code, "policy_drift")
        self.assertEqual(self.fixture_policy["registered_adapters"], [])
        self.assertTrue(all(not item["enabled"] for item in self.fixture_policy["seat_policies"]))

    def test_pending_and_claimed_wake_lease_excludes_source_and_later_ack(self) -> None:
        self.initialize()
        self.arm()
        source = self.send(body="wake source")
        intent = self.store.discover("claude-code")["intent"]
        later = self.send(body="later source")
        cursor_before = self.panel._read_cursor("claude-code")
        for message_id in (source["id"], later["id"]):
            with self.subTest(state="pending", message_id=message_id):
                with self.assertRaises(collaboration_panel.WakeLeaseConflict) as conflict:
                    self.panel.acknowledge(seat_id="claude-code", message_id=message_id)
                self.assertEqual(
                    conflict.exception.details["wakeup_error_code"],
                    "wake_lease_conflict",
                )
                self.assertEqual(self.panel._read_cursor("claude-code"), cursor_before)
        self.claim(intent)
        for message_id in (source["id"], later["id"]):
            with self.subTest(state="claimed", message_id=message_id):
                with self.assertRaises(collaboration_panel.WakeLeaseConflict):
                    self.panel.acknowledge(seat_id="claude-code", message_id=message_id)
                self.assertEqual(self.panel._read_cursor("claude-code"), cursor_before)

    def test_open_intent_and_claim_are_never_counted_as_quiet_cycles(self) -> None:
        self.initialize()
        self.arm(quiet_cycles=1)
        self.send(body="未完成工作不是安静周期")
        intent = self.store.discover("claude-code")["intent"]
        pending = self.store.discover("claude-code")
        self.assertIn("open_intent", pending["reason"])
        window = self.store.snapshot()["wake_window"]
        self.assertEqual(window["state"], "active")
        self.assertEqual(window["quiet_cycles"], 0)
        self.claim(intent)
        claimed = self.store.discover("claude-code")
        self.assertIn("open_intent", claimed["reason"])
        window = self.store.snapshot()["wake_window"]
        self.assertEqual(window["state"], "active")
        self.assertEqual(window["quiet_cycles"], 0)

    def test_durable_created_response_loss_replays_same_pending_without_claim_leak(self) -> None:
        self.initialize()
        self.arm(max_wakes=1)
        self.send(body="created 已落盘但响应丢失")

        def lose_response(stage: str) -> None:
            if stage == "after_created_event":
                raise RuntimeError("simulated response loss")

        with mock.patch.object(self.store, "_fault_hook", side_effect=lose_response):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                self.store.discover("claude-code")
        self.assertTrue(self.store.validate()["ok"])
        before_records = self.store._verify_chain(self.store.intents_path, "record_hash")
        original_pending = next(
            value
            for value in self.store._intent_states().values()
            if value.get("status") == "pending"
        )

        replay = self.store.discover("claude-code")
        self.assertEqual(replay["reason"], "open_intent_replayed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["intent"]["intent_id"], original_pending["intent_id"])
        self.assertEqual(
            len(self.store._verify_chain(self.store.intents_path, "record_hash")),
            len(before_records),
        )
        self.assertEqual(self.store.snapshot()["wake_window"]["state"], "draining")

        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        proof_one = agent_wakeup.build_claim_proof(replay["intent"], key)
        proof_two = agent_wakeup.build_claim_proof(replay["intent"], key)
        self.assertEqual(proof_one, proof_two)
        first_claim = self.store.claim(
            intent_id=replay["intent"]["intent_id"],
            adapter_id=replay["intent"]["adapter_id"],
            adapter_sha256=replay["intent"]["adapter_sha256"],
            claim_proof_hmac=proof_one,
        )
        repeated_claim = self.store.claim(
            intent_id=replay["intent"]["intent_id"],
            adapter_id=replay["intent"]["adapter_id"],
            adapter_sha256=replay["intent"]["adapter_sha256"],
            claim_proof_hmac=proof_two,
        )
        self.assertTrue(repeated_claim["idempotent_replay"])
        self.assertEqual(repeated_claim["claim_id"], first_claim["claim_id"])
        self.assertEqual(repeated_claim["nonce"], first_claim["nonce"])
        claimed_discovery = self.store.discover("claude-code")
        self.assertIsNone(claimed_discovery["intent"])
        self.assertEqual(claimed_discovery["reason"], "window_draining")
        records = self.store._verify_chain(self.store.intents_path, "record_hash")
        self.assertEqual(sum(item["action"] == "created" for item in records), 1)
        self.assertEqual(sum(item["action"] == "claimed" for item in records), 1)

    def test_created_claimed_and_disposition_event_splits_are_detected_and_replayed(self) -> None:
        self.initialize()
        self.arm()
        self.send(body="created split")

        def fail_created(stage: str) -> None:
            if stage == "after_created_intent":
                raise RuntimeError("created split")

        with mock.patch.object(self.store, "_fault_hook", side_effect=fail_created):
            with self.assertRaisesRegex(RuntimeError, "created split"):
                self.store.discover("claude-code")
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()
        recovered_discovery = self.store.discover("claude-code")
        self.assertEqual(recovered_discovery["reason"], "intent_recovered")
        self.assertTrue(recovered_discovery["idempotent_replay"])
        intent = recovered_discovery["intent"]
        self.assertTrue(self.store.validate()["ok"])

        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        proof = agent_wakeup.build_claim_proof(intent, key)

        def fail_claimed(stage: str) -> None:
            if stage == "after_claimed_intent":
                raise RuntimeError("claimed split")

        with mock.patch.object(self.store, "_fault_hook", side_effect=fail_claimed):
            with self.assertRaisesRegex(RuntimeError, "claimed split"):
                self.store.claim(
                    intent_id=intent["intent_id"],
                    adapter_id=intent["adapter_id"],
                    adapter_sha256=intent["adapter_sha256"],
                    claim_proof_hmac=proof,
                )
        claimed_state = self.store._intent_states()[intent["intent_id"]]
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()
        recovered_claim = self.store.claim(
            intent_id=intent["intent_id"],
            adapter_id=intent["adapter_id"],
            adapter_sha256=intent["adapter_sha256"],
            claim_proof_hmac=proof,
        )
        self.assertTrue(recovered_claim["idempotent_replay"])
        self.assertEqual(recovered_claim["claim_id"], claimed_state["claim_id"])
        self.assertTrue(self.store.validate()["ok"])

        self.clock.advance(181)
        self.store.discover("claude-code")
        held = self.store._intent_states()[intent["intent_id"]]
        self.assertEqual(held["status"], "root_hold")

        def fail_disposition(stage: str) -> None:
            if stage == "after_disposition_intent":
                raise RuntimeError("disposition split")

        disposition_args = {
            "intent_id": intent["intent_id"],
            "source_message_id": intent["source_message_id"],
            "window_id": intent["window_id"],
            "reason": "审计后单次重试",
        }
        with mock.patch.object(self.store, "_fault_hook", side_effect=fail_disposition):
            with self.assertRaisesRegex(RuntimeError, "disposition split"):
                self.store.dispose_hold(**disposition_args)
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()
        recovered_disposition = self.store.dispose_hold(**disposition_args)
        self.assertTrue(recovered_disposition["idempotent_replay"])
        self.assertTrue(self.store.validate()["ok"])
        retry = self.store.discover("claude-code")["intent"]
        self.assertEqual(retry["attempt"], 2)

        records = self.store._verify_chain(self.store.intents_path, "record_hash")
        events = self.store._verify_chain(self.store.events_path, "event_hash")
        for intent_action, event_action in (
            ("created", "wake_intent_created"),
            ("claimed", "wake_intent_claimed"),
            ("user_retry_authorized", "wake_hold_disposition"),
        ):
            self.assertEqual(
                sum(
                    record.get("intent_id") == intent["intent_id"]
                    and record.get("action") == intent_action
                    for record in records
                ),
                1,
            )
            self.assertEqual(
                sum(
                    event.get("payload", {}).get("intent_id") == intent["intent_id"]
                    and event.get("action") == event_action
                    for event in events
                ),
                1,
            )

    def test_preexisting_old_consumer_cursor_creates_dual_consumer_hold(self) -> None:
        self.initialize()
        self.arm()
        source = self.send(body="旧消费者先 ACK")
        self.panel.acknowledge(seat_id="claude-code", message_id=source["id"])
        intent = self.store.discover("claude-code")["intent"]
        claim = self.claim(intent)
        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        receipt = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="sent_and_verified",
            delivery_ref="old-consumer-conflict",
            now=self.clock(),
        )
        wake_before = json.loads(self.store.cursors_path.read_text(encoding="utf-8"))
        def fail_hold(stage: str) -> None:
            if stage == "after_dual_consumer_hold_intent":
                raise RuntimeError("dual hold split")

        with mock.patch.object(self.store, "_fault_hook", side_effect=fail_hold):
            with self.assertRaisesRegex(RuntimeError, "dual hold split"):
                self.store.complete(receipt)
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()
        with self.assertRaises(agent_wakeup.GateBlocked) as conflict:
            self.store.complete(receipt)
        self.assertEqual(conflict.exception.code, "dual_consumer_conflict")
        self.assertTrue(self.store.validate()["ok"])
        self.assertEqual(
            json.loads(self.store.cursors_path.read_text(encoding="utf-8")),
            wake_before,
        )
        self.assertEqual(
            self.store._intent_states()[intent["intent_id"]]["status"],
            "dual_consumer_hold",
        )
        self.assertEqual(self.store._load_completion_journal()["entries"], {})
        row = next(item for item in self.store.snapshot()["seats"] if item["seat_id"] == "claude-code")
        self.assertTrue(row["awaiting_user_disposition"])

    def test_timeout_and_terminal_hold_event_splits_are_detected_and_replayed(self) -> None:
        self.initialize()
        self.arm()
        self.send(body="timeout split")
        timeout_intent = self.store.discover("claude-code")["intent"]
        self.claim(timeout_intent)
        self.clock.advance(181)

        def fail_timeout(stage: str) -> None:
            if stage == "after_timed_out_intent":
                raise RuntimeError("timeout split")

        with mock.patch.object(self.store, "_fault_hook", side_effect=fail_timeout):
            with self.assertRaisesRegex(RuntimeError, "timeout split"):
                self.store.discover("claude-code")
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()
        timeout_replay = self.store.discover("claude-code")
        self.assertIn("root_hold", timeout_replay["reason"])
        self.assertTrue(self.store.validate()["ok"])

        self.store.dispose_hold(
            intent_id=timeout_intent["intent_id"],
            source_message_id=timeout_intent["source_message_id"],
            window_id=timeout_intent["window_id"],
            reason="继续失败重试测试",
        )
        for attempt in range(2, 5):
            intent = self.store.discover("claude-code")["intent"]
            claim = self.claim(intent)
            receipt = agent_wakeup.build_receipt(
                claim,
                receipt_key=agent_wakeup.load_host_receipt_key(self.store.receipt_key_path),
                status="failed",
                failure_code=f"terminal_split_{attempt}",
                now=self.clock(),
            )
            if attempt < 4:
                self.store.complete(receipt)
                self.clock.advance((60, 180)[attempt - 2])
                continue

            def fail_terminal(stage: str) -> None:
                if stage == "after_terminal_hold_intent":
                    raise RuntimeError("terminal hold split")

            with mock.patch.object(self.store, "_fault_hook", side_effect=fail_terminal):
                with self.assertRaisesRegex(RuntimeError, "terminal hold split"):
                    self.store.complete(receipt)
            with self.assertRaises(agent_wakeup.ChainError):
                self.store.validate()
            replay = self.store.complete(receipt)
            self.assertTrue(replay["terminal_hold"])
            self.assertTrue(self.store.validate()["ok"])

    def test_completion_recovery_is_idempotent_at_every_commit_boundary(self) -> None:
        stages = (
            "after_intent_append",
            "after_event_append",
            "after_wakeup_cursor",
            "after_panel_cursor",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                root = Path(tempfile.mkdtemp(prefix="sublation-completion-fault-"))
                try:
                    panel = collaboration_panel.PanelStore(root)
                    panel.initialize(project_name="fault", user_name="root")
                    clock = Clock()
                    store = agent_wakeup.WakeupStore(root, clock=clock)
                    store.initialize(self.policy)
                    store.arm_window(deadline=clock.future(3500), quiet_cycles=20, max_wakes=20)
                    source = panel.send_message(
                        speaker="user",
                        audience="claude-code",
                        body="故障恢复",
                    )
                    intent = store.discover("claude-code")["intent"]
                    key = agent_wakeup.load_host_receipt_key(store.receipt_key_path)
                    claim = store.claim(
                        intent_id=intent["intent_id"],
                        adapter_id=intent["adapter_id"],
                        adapter_sha256=intent["adapter_sha256"],
                        claim_proof_hmac=agent_wakeup.build_claim_proof(intent, key),
                    )
                    receipt = agent_wakeup.build_receipt(
                        claim,
                        receipt_key=key,
                        status="sent_and_verified",
                        delivery_ref="fault-" + stage,
                        now=clock(),
                    )
                    fired = False

                    def fail_once(current: str) -> None:
                        nonlocal fired
                        if current == stage and not fired:
                            fired = True
                            raise RuntimeError("simulated crash at " + stage)

                    with mock.patch.object(store, "_fault_hook", side_effect=fail_once):
                        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                            store.complete(receipt)
                    with self.assertRaises(agent_wakeup.GateBlocked) as incomplete:
                        store.validate()
                    self.assertEqual(incomplete.exception.code, "completion_recovery_required")
                    row = next(
                        item
                        for item in store.snapshot()["seats"]
                        if item["seat_id"] == "claude-code"
                    )
                    self.assertTrue(row["completion_recovery_required"])
                    recovered = store.complete(receipt)
                    self.assertTrue(recovered["idempotent_replay"])
                    self.assertTrue(store.validate()["ok"])
                    self.assertEqual(panel.inbox("claude-code")["pending_count"], 0)
                    completion_records = [
                        record
                        for record in store._verify_chain(store.intents_path, "record_hash")
                        if record.get("intent_id") == intent["intent_id"]
                        and record.get("action") == "completed"
                    ]
                    self.assertEqual(len(completion_records), 1)
                    self.assertEqual(
                        panel._read_cursor("claude-code")["last_seen_id"],
                        source["id"],
                    )
                finally:
                    shutil.rmtree(root)

    def test_complete_serializes_concurrent_legacy_ack(self) -> None:
        self.initialize()
        self.arm()
        source = self.send(body="并发 ACK")
        intent = self.store.discover("claude-code")["intent"]
        claim = self.claim(intent)
        receipt = agent_wakeup.build_receipt(
            claim,
            receipt_key=agent_wakeup.load_host_receipt_key(self.store.receipt_key_path),
            status="sent_and_verified",
            delivery_ref="concurrent-ack",
            now=self.clock(),
        )
        prepared = threading.Event()
        release = threading.Event()
        complete_failures: list[BaseException] = []
        ack_failures: list[BaseException] = []

        def barrier(stage: str) -> None:
            if stage == "after_journal_prepare":
                prepared.set()
                if not release.wait(5):
                    raise RuntimeError("completion barrier timeout")

        def finish() -> None:
            try:
                self.store.complete(receipt)
            except BaseException as exc:
                complete_failures.append(exc)

        def ack() -> None:
            try:
                self.panel.acknowledge(seat_id="claude-code", message_id=source["id"])
            except BaseException as exc:
                ack_failures.append(exc)

        with mock.patch.object(self.store, "_fault_hook", side_effect=barrier):
            complete_thread = threading.Thread(target=finish, daemon=True)
            complete_thread.start()
            self.assertTrue(prepared.wait(5))
            ack_thread = threading.Thread(target=ack, daemon=True)
            ack_thread.start()
            self.assertTrue(ack_thread.is_alive())
            release.set()
            complete_thread.join(5)
            ack_thread.join(5)
        self.assertEqual(complete_failures, [])
        self.assertEqual(len(ack_failures), 1)
        self.assertIsInstance(ack_failures[0], collaboration_panel.ValidationError)
        self.assertEqual(self.panel.inbox("claude-code")["pending_count"], 0)

    def test_quiet_or_wake_limit_enters_draining_and_close_receipt_is_hash_bound(self) -> None:
        self.initialize()
        self.arm(quiet_cycles=1, max_wakes=5)
        idle = self.store.discover()
        self.assertEqual(idle["reason"], "idle_silent")
        self.assertEqual(self.store.snapshot()["wake_window"]["state"], "draining")
        receipt = self.store.close_window()
        payload = dict(receipt)
        claimed = payload.pop("close_receipt_sha256")
        self.assertEqual(claimed, agent_wakeup._sha256(payload))
        window = self.store.snapshot()["wake_window"]
        self.assertEqual(window["state"], "closed")
        self.assertEqual(window["close_receipt_sha256"], claimed)

    def test_close_rejects_pending_intent_and_active_lease_until_drained(self) -> None:
        self.initialize()
        self.arm(max_wakes=1)
        self.send()
        intent = self.store.discover("claude-code")["intent"]
        self.assertEqual(self.store.snapshot()["wake_window"]["state"], "draining")
        with self.assertRaises(agent_wakeup.GateBlocked) as pending:
            self.store.close_window()
        self.assertEqual(pending.exception.code, "window_not_drained")
        claim = self.claim(intent)
        with self.assertRaises(agent_wakeup.GateBlocked):
            self.store.close_window()
        self.complete(claim, status="sent_and_verified")
        receipt = self.store.close_window()
        self.assertEqual(receipt["pending_intents"], 0)
        self.assertEqual(receipt["active_leases"], 0)

    def test_hash_chain_and_cursor_projection_tamper_are_detected(self) -> None:
        self.initialize()
        lines = self.store.events_path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["action"] = "tampered"
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.store.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(agent_wakeup.ChainError):
            self.store.validate()

    def test_policy_receipt_and_source_reject_commands_secrets_raw_content_and_ignored(self) -> None:
        unsafe = copy.deepcopy(self.policy)
        unsafe["registered_adapters"][0]["argv"] = ["claude", "--dangerous"]
        with self.assertRaises(agent_wakeup.WakeupError) as command:
            self.initialize(unsafe)
        self.assertEqual(command.exception.code, "unsafe_field")

        # Recreate because a rejected pre-state policy did not initialize wakeup state.
        self.initialize()
        self.arm()
        self.send()
        claim = self.claim(self.store.discover("claude-code")["intent"])
        key = agent_wakeup.load_host_receipt_key(self.store.receipt_key_path)
        ignored = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="failed",
            failure_code="x",
            now=self.clock(),
        )
        ignored["status"] = "ignored"
        ignored["receipt_sha256"] = agent_wakeup.receipt_sha256(ignored)
        with self.assertRaises(agent_wakeup.WakeupError):
            self.store.complete(ignored)
        secret = agent_wakeup.build_receipt(
            claim,
            receipt_key=key,
            status="failed",
            failure_code="x",
            now=self.clock(),
        )
        secret["secret"] = "not-allowed"
        secret["receipt_sha256"] = agent_wakeup.receipt_sha256(secret)
        with self.assertRaises(agent_wakeup.WakeupError) as rejected:
            self.store.complete(secret)
        self.assertEqual(rejected.exception.code, "unsafe_field")

        source = (ROOT / "scripts" / "agent_wakeup.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import socket", source)
        self.assertNotIn("import urllib", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("Popen(", source)

    def test_policy_and_receipt_schema_files_are_strict_contracts(self) -> None:
        policy_schema = json.loads((ROOT / "schemas" / "agent-wakeup-policy-v1.json").read_text(encoding="utf-8"))
        receipt_schema = json.loads((ROOT / "schemas" / "agent-wakeup-receipt-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(policy_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(policy_schema["additionalProperties"])
        self.assertFalse(receipt_schema["additionalProperties"])
        self.assertEqual(self.policy["retry_policy"]["backoff_seconds"], [60, 180, 600])
        self.assertEqual(self.policy["retry_policy"]["max_attempts"], 4)
        self.assertEqual(self.policy["window_policy"]["default_state"], "closed")
        self.assertEqual(self.fixture_policy["registered_adapters"], [])
        self.assertTrue(self.fixture_policy["pending_host_installs"])
        self.assertTrue(all(not item["enabled"] for item in self.fixture_policy["seat_policies"]))


if __name__ == "__main__":
    unittest.main()
