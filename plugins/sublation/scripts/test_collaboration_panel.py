#!/usr/bin/env python3
"""Sublation V5 项目本地多 Agent 协作面板定向测试。"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "collaboration_panel.py"
SPEC = importlib.util.spec_from_file_location("collaboration_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("无法加载 collaboration_panel.py")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class CollaborationPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sublation-panel-test-")
        self.project = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialized(self, **kwargs: object):
        store = panel.PanelStore(self.project)
        store.initialize(**kwargs)
        return store

    def test_initialization_has_one_active_user_default_12_and_explicit_32(self) -> None:
        store = self.initialized(user_name="root")
        snapshot = store.snapshot()
        self.assertEqual(store.state_dir.name, ".sublation-panel")
        self.assertEqual(snapshot["roster"]["max_agent_seats"], 12)
        users = [seat for seat in snapshot["roster"]["seats"] if seat["kind"] == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["id"], "user")
        self.assertEqual(users[0]["display_name"], "root")
        self.assertTrue(users[0]["active"])
        self.assertTrue(users[0]["visible"])

        with tempfile.TemporaryDirectory(prefix="sublation-panel-max-") as second:
            second_store = panel.PanelStore(second)
            second_store.initialize(max_agent_seats=32)
            self.assertEqual(second_store.snapshot()["roster"]["max_agent_seats"], 32)
        with tempfile.TemporaryDirectory(prefix="sublation-panel-too-many-") as third:
            with self.assertRaises(panel.ValidationError):
                panel.PanelStore(third).initialize(max_agent_seats=33)

        invalid = copy.deepcopy(snapshot["roster"])
        invalid["seats"][1]["kind"] = "user"
        with self.assertRaises(panel.PanelError):
            panel.PanelStore._validate_roster_projection(invalid)

    def test_invalid_user_name_is_rejected_before_state_directory_creation(self) -> None:
        store = panel.PanelStore(self.project)
        with self.assertRaises(panel.ValidationError):
            store.initialize(user_name="root\n伪造")
        self.assertFalse(store.state_dir.exists())

    def test_wakeup_projection_is_default_off_until_explicit_initialization(self) -> None:
        store = self.initialized()
        wakeup = store.snapshot()["wakeup"]
        self.assertFalse(wakeup["initialized"])
        self.assertEqual(wakeup["status"], "default_off")
        self.assertEqual(wakeup["wake_window"]["state"], "closed")
        self.assertEqual(wakeup["totals"], {"enabled": 0, "disabled": 3, "open_intents": 0})
        self.assertTrue(all(not seat["enabled"] for seat in wakeup["seats"]))
        self.assertFalse(wakeup["boundary"]["direct_process_launch"])
        self.assertFalse(wakeup["boundary"]["external_network"])

    def test_panel_projects_initialized_wakeup_ledger_without_starting_an_agent(self) -> None:
        store = self.initialized()
        wake_module_path = ROOT / "scripts" / "agent_wakeup.py"
        wake_spec = importlib.util.spec_from_file_location("agent_wakeup_panel_integration", wake_module_path)
        self.assertIsNotNone(wake_spec)
        self.assertIsNotNone(wake_spec.loader)
        wake_module = importlib.util.module_from_spec(wake_spec)
        wake_spec.loader.exec_module(wake_module)
        wake_module.WakeupStore(self.project).initialize(
            ROOT / "fixtures" / "collaboration-panel" / "wakeup-policies.json"
        )

        wakeup = store.snapshot()["wakeup"]
        self.assertTrue(wakeup["initialized"])
        self.assertEqual(wakeup["status"], "ready")
        self.assertEqual(wakeup["wake_window"]["state"], "closed")
        self.assertEqual(wakeup["totals"]["enabled"], 0)
        self.assertEqual(
            {item["seat_id"] for item in wakeup["policy"]["pending_host_installs"]},
            {"claude-code", "hermes"},
        )
        self.assertFalse(wakeup["boundary"]["direct_process_launch"])
        self.assertEqual(wakeup["event_chain"]["valid"], True)

    def test_retirement_rejects_pending_wakeup_intent_or_active_lease(self) -> None:
        store = self.initialized()
        wake_module_path = ROOT / "scripts" / "agent_wakeup.py"
        wake_spec = importlib.util.spec_from_file_location("agent_wakeup_retirement_guard", wake_module_path)
        self.assertIsNotNone(wake_spec)
        self.assertIsNotNone(wake_spec.loader)
        wake_module = importlib.util.module_from_spec(wake_spec)
        wake_spec.loader.exec_module(wake_module)
        wake_store = wake_module.WakeupStore(self.project)
        policy = json.loads(
            (ROOT / "fixtures" / "collaboration-panel" / "wakeup-policies.json").read_text(
                encoding="utf-8"
            )
        )
        adapter_sha256 = "sha256:" + hashlib.sha256(wake_module_path.read_bytes()).hexdigest()
        policy["registered_adapters"] = [
            {
                "adapter_id": "claude-code-watcher-auto-v1",
                "adapter_sha256": adapter_sha256,
                "kind": "watcher_auto",
                "allowed_seat_ids": ["claude-code"],
            }
        ]
        policy["pending_host_installs"] = [
            item
            for item in policy["pending_host_installs"]
            if item["seat_id"] != "claude-code"
        ]
        for item in policy["seat_policies"]:
            if item["seat_id"] == "claude-code":
                item["enabled"] = True
                item["mode"] = "intent_only"
                item["adapter_id"] = "claude-code-watcher-auto-v1"
        wake_store.initialize(policy)
        wake_store.arm_window(
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            quiet_cycles=2,
            max_wakes=2,
        )
        message = store.send_message(speaker="user", audience="claude-code", body="合成只读唤醒测试")
        store.acknowledge(seat_id="claude-code", message_id=message["id"])
        intent = wake_store.discover("claude-code")["intent"]
        self.assertIsNotNone(intent)

        with self.assertRaises(panel.SeatGuardError) as pending_guard:
            store.disable_agent(seat_id="claude-code", expected_revision=1)
        self.assertEqual(pending_guard.exception.details["wakeup_intent_id"], intent["intent_id"])

        receipt_key = wake_module.load_host_receipt_key(wake_store.receipt_key_path)
        wake_store.claim(
            intent_id=intent["intent_id"],
            adapter_id=intent["adapter_id"],
            adapter_sha256=intent["adapter_sha256"],
            claim_proof_hmac=wake_module.build_claim_proof(intent, receipt_key),
        )
        with self.assertRaises(panel.SeatGuardError) as lease_guard:
            store.disable_agent(seat_id="claude-code", expected_revision=1)
        self.assertEqual(lease_guard.exception.details["wakeup_last_outcome"], "claimed")

    def test_fixed_user_cannot_be_hidden_retired_or_restored(self) -> None:
        store = self.initialized()
        with self.assertRaises(panel.SeatGuardError):
            store.set_visibility(seat_id="user", visible=False, expected_revision=1)
        with self.assertRaises(panel.SeatGuardError):
            store.disable_agent(seat_id="user", expected_revision=1)
        with self.assertRaises(panel.SeatGuardError):
            store.restore_agent(seat_id="user", expected_revision=1)
        snapshot = store.snapshot()
        invalid = copy.deepcopy(snapshot["roster"])
        invalid["seats"][0]["active"] = False
        with self.assertRaises(panel.PanelError):
            panel.PanelStore._validate_roster_projection(invalid)

    def test_agent_limit_revision_id_tombstone_and_new_cursor_baseline(self) -> None:
        store = self.initialized(max_agent_seats=5)
        before = store.send_message(speaker="user", audience="all", body="加入前广播")
        revision = store.snapshot()["roster"]["revision"]
        result = store.add_agent(
            seat_id="specialist-a",
            display_name="专项复核 A",
            expected_revision=revision,
        )
        cursor = store._read_cursor("specialist-a")
        self.assertEqual(cursor["last_seen_id"], before["id"])
        self.assertEqual(cursor["joined_after_message_id"], before["id"])
        self.assertEqual(store.inbox("specialist-a")["pending_count"], 0)
        after = store.send_message(speaker="user", audience="all", body="加入后广播")
        self.assertEqual([item["id"] for item in store.inbox("specialist-a")["messages"]], [after["id"]])
        with self.assertRaises(panel.RevisionConflict):
            store.add_agent(
                seat_id="stale-agent",
                display_name="过期写入",
                expected_revision=revision,
            )
        self.assertEqual(result["seat"]["id"], "specialist-a")

        with tempfile.TemporaryDirectory(prefix="sublation-panel-limit-") as limited:
            limited_store = panel.PanelStore(limited)
            limited_store.initialize(max_agent_seats=3)
            with self.assertRaises(panel.SeatGuardError):
                limited_store.add_agent(
                    seat_id="over-limit",
                    display_name="超限",
                    expected_revision=1,
                )

        with tempfile.TemporaryDirectory(prefix="sublation-panel-tombstone-") as retired:
            retired_store = panel.PanelStore(retired)
            retired_store.initialize()
            retired_store.disable_agent(seat_id="claude-code", expected_revision=1)
            with self.assertRaises(panel.SeatGuardError):
                retired_store.add_agent(
                    seat_id="claude-code",
                    display_name="伪复用",
                    expected_revision=2,
                )

    def test_hide_show_retire_guards_and_restore_skips_retired_broadcasts(self) -> None:
        store = self.initialized()
        original_cursor = store._read_cursor("hermes")
        hidden = store.set_visibility(seat_id="hermes", visible=False, expected_revision=1)
        self.assertTrue(hidden["seat"]["active"])
        self.assertFalse(hidden["seat"]["visible"])
        queued = store.send_message(speaker="user", audience="all", body="隐藏时仍投递")
        self.assertEqual(store.inbox("hermes")["pending_count"], 1)
        with self.assertRaises(panel.SeatGuardError) as pending_guard:
            store.disable_agent(seat_id="hermes", expected_revision=2)
        self.assertIn(queued["id"], pending_guard.exception.details["pending_message_ids"])
        store.acknowledge(seat_id="hermes", message_id=queued["id"])
        store.set_visibility(seat_id="hermes", visible=True, expected_revision=2)

        task = store.add_task(title="仍需处理", owner="hermes")
        with self.assertRaises(panel.SeatGuardError) as task_guard:
            store.disable_agent(seat_id="hermes", expected_revision=3)
        self.assertIn(task["id"], task_guard.exception.details["open_task_ids"])
        store.update_task(task_id=task["id"], status="done")
        retired = store.disable_agent(seat_id="hermes", expected_revision=3)
        self.assertFalse(retired["seat"]["active"])
        self.assertFalse(retired["seat"]["visible"])

        during_retirement = store.send_message(speaker="user", audience="all", body="停用期广播")
        restored = store.restore_agent(seat_id="hermes", expected_revision=4)
        self.assertEqual(restored["seat"]["id"], "hermes")
        self.assertTrue(restored["seat"]["active"])
        self.assertTrue(restored["seat"]["visible"])
        self.assertEqual(restored["restored_cursor"]["last_seen_id"], during_retirement["id"])
        self.assertEqual(
            restored["restored_cursor"]["joined_after_message_id"],
            original_cursor["joined_after_message_id"],
        )
        self.assertEqual(store.inbox("hermes")["pending_count"], 0)
        self.assertEqual(
            sum(seat["id"] == "hermes" for seat in store.snapshot()["roster"]["seats"]),
            1,
        )
        last_event = read_jsonl(store.panel_events_path)[-1]
        self.assertEqual(last_event["action"], "restore")
        self.assertEqual(last_event["metadata"]["cursor_resumed_after_message_id"], during_retirement["id"])

    def test_restore_refuses_to_exceed_project_active_seat_cap(self) -> None:
        store = self.initialized(max_agent_seats=3)
        store.disable_agent(seat_id="claude-code", expected_revision=1)
        store.add_agent(
            seat_id="replacement",
            display_name="替补席",
            expected_revision=2,
        )
        with self.assertRaises(panel.SeatGuardError):
            store.restore_agent(seat_id="claude-code", expected_revision=3)

    def test_panel_and_roster_event_hash_chains_and_projection_hashes(self) -> None:
        store = self.initialized()
        store.set_visibility(seat_id="codex", visible=False, expected_revision=1)
        store.set_visibility(seat_id="codex", visible=True, expected_revision=2)

        previous = None
        projections: dict[int, dict] = {}
        for event in read_jsonl(store.panel_events_path):
            unsigned = dict(event)
            event_hash = unsigned.pop("event_sha256")
            self.assertEqual(event_hash, panel.sha256_tag(unsigned))
            self.assertEqual(event["prev_event_sha256"], previous)
            self.assertEqual(event["after_sha256"], panel.sha256_tag(event["roster_projection"]))
            if event["previous_revision"] is not None:
                self.assertEqual(event["before_sha256"], panel.sha256_tag(projections[event["previous_revision"]]))
            projections[event["revision"]] = event["roster_projection"]
            previous = event_hash

        previous = None
        for event in read_jsonl(store.roster_events_path):
            unsigned = dict(event)
            event_hash = unsigned.pop("event_sha256")
            self.assertEqual(event_hash, panel.sha256_tag(unsigned))
            self.assertEqual(event["previous_event_sha256"], previous)
            self.assertEqual(event["after_sha256"], panel.sha256_tag(projections[event["revision"]]))
            if event["previous_revision"] is not None:
                self.assertEqual(event["before_sha256"], panel.sha256_tag(projections[event["previous_revision"]]))
            previous = event_hash

        events = read_jsonl(store.panel_events_path)
        events[-1]["reason"] = "篡改"
        store.panel_events_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in events),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(panel.PanelError, "event_sha256"):
            store.snapshot()

    def test_roster_event_hash_tampering_is_rejected(self) -> None:
        store = self.initialized()
        events = read_jsonl(store.roster_events_path)
        events[-1]["reason"] = "篡改兼容事件"
        store.roster_events_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in events),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(panel.PanelError, "roster-events event_sha256"):
            store.snapshot()

    def test_routing_is_four_layers_and_advanced_states_fail_closed(self) -> None:
        store = self.initialized()
        suggested = store.record_routing_status(
            state="suggested",
            request_id="opaque-1",
            skill_ids=["skill-sublation"],
            agent_id="codex",
        )
        self.assertFalse(suggested["raw_prompt_stored"] if "raw_prompt_stored" in suggested else False)
        fake_receipt = {
            "schema_version": "skill-sublation.panel-host-routing-receipt.v1",
            "receipt_id": "fake-1",
            "request_id": "opaque-1",
            "seat_id": "codex",
            "skill_ids": ["skill-sublation"],
            "baseline_sha256": "sha256:" + "1" * 64,
            "effective_sha256": "sha256:" + "1" * 64,
            "invocation_performed": True,
            "completion_status": "completed",
        }
        fake_receipt["receipt_sha256"] = panel.sha256_tag(fake_receipt)
        with self.assertRaises(panel.HostAdapterRequired) as blocked:
            store.record_routing_status(
                state="completed",
                request_id="opaque-1",
                skill_ids=["skill-sublation"],
                agent_id="codex",
                host_receipt=fake_receipt,
            )
        self.assertEqual(blocked.exception.code, "requires_future_signed_host_adapter")
        with self.assertRaises(panel.ValidationError):
            store.record_routing_payload({"state": "suggested", "request_id": "opaque-2", "raw_prompt": "秘密"})

        panel._append_jsonl(
            store.routing_path,
            {
                "schema_version": panel.ROUTING_DISPLAY_SCHEMA,
                "state": "completed",
                "request_id": "forged",
                "receipt_validated": True,
                "host_invocation_performed": True,
                "note": "手工伪造",
            },
        )
        routing = store.snapshot()["routing"]
        self.assertEqual(
            [layer["id"] for layer in routing["layers"]],
            [
                "metadata_classification",
                "advisory_recommendation",
                "host_selection",
                "actual_invocation",
            ],
        )
        self.assertEqual(routing["layers"][-1]["status"], "not_performed")
        self.assertIsNone(routing["layers"][-1]["record"])
        self.assertIsNone(routing["latest_by_state"]["completed"])
        self.assertFalse(routing["raw_prompt_stored"])
        self.assertFalse(routing["authority_granted_by_router"])

    def test_dynamic_roster_api_and_loopback_host_origin_guards(self) -> None:
        store = self.initialized()
        server = panel.make_server(store, port=0)
        self.assertEqual(server.server_address[0], panel.LOOPBACK_HOST)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request("GET", "/api/agents", headers={"X-Panel-Token": store.api_token()})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["roster"]["revision"], 1)
            self.assertEqual(set(payload["agent_runtime"]), {"codex", "hermes", "claude-code"})
            connection.close()

            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["project_id"], store.snapshot()["project"]["id"])
            self.assertTrue(payload["project_root_sha256"].startswith("sha256:"))
            self.assertTrue(payload["capabilities"]["wakeup_intent_gate"])
            self.assertFalse(payload["capabilities"]["direct_agent_launch"])
            connection.close()

            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request("GET", "/api/bootstrap")
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["token_scope"], "same_origin_panel_csrf_only")
            self.assertFalse(payload["watcher_trust_root"])
            token = payload["api_token"]
            connection.close()

            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request("GET", "/api/wakeup", headers={"X-Panel-Token": token})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["wakeup"]["status"], "default_off")
            connection.close()

            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request("GET", "/api/health", headers={"Host": "localhost"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()

            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            connection.request(
                "GET",
                "/api/health",
                headers={"Host": f"127.0.0.1:{port}", "Origin": "http://example.com"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_hide_show_retire_restore_lifecycle(self) -> None:
        store = self.initialized()
        server = panel.make_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def post(path: str, revision: int) -> tuple[int, dict]:
            connection = http.client.HTTPConnection(panel.LOOPBACK_HOST, port, timeout=3)
            body = json.dumps({"expected_revision": revision})
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json", "X-Panel-Token": store.api_token()},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            status = response.status
            connection.close()
            return status, payload

        try:
            status, hidden = post("/api/agents/codex/hide", 1)
            self.assertEqual(status, 200)
            self.assertFalse(hidden["seat"]["visible"])
            status, shown = post("/api/agents/codex/show", 2)
            self.assertEqual(status, 200)
            self.assertTrue(shown["seat"]["visible"])
            status, retired = post("/api/agents/codex/retire", 3)
            self.assertEqual(status, 200)
            self.assertFalse(retired["seat"]["active"])
            status, restored = post("/api/agents/codex/restore", 4)
            self.assertEqual(status, 200)
            self.assertTrue(restored["seat"]["active"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_cli_completed_routing_is_explicitly_fail_closed(self) -> None:
        self.initialized()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = panel.main(
                [
                    "routing-add",
                    "--project",
                    str(self.project),
                    "--state",
                    "completed",
                    "--request-id",
                    "opaque-cli",
                    "--agent-id",
                    "codex",
                ]
            )
        self.assertEqual(status, 2)
        self.assertIn("requires_future_signed_host_adapter", stderr.getvalue())

    def test_cli_contract_and_ui_are_dynamic_not_four_seat_hardcoded(self) -> None:
        args = panel.build_parser().parse_args(
            [
                "add-seat",
                "--project",
                str(self.project),
                "--seat-id",
                "specialist-a",
                "--display-name",
                "专项复核 A",
                "--expected-revision",
                "1",
            ]
        )
        self.assertEqual(args.seat_id, "specialist-a")
        init_args = panel.build_parser().parse_args(["init", "--project", str(self.project)])
        self.assertEqual(init_args.user_name, "root")
        self.assertEqual(init_args.max_agent_seats, 12)

        app = (ROOT / "assets" / "multi-agent-panel" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "assets" / "multi-agent-panel" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("codex", app.lower())
        self.assertNotIn("hermes", app.lower())
        self.assertNotIn("claude-code", app.lower())
        self.assertIn("appState.snapshot.roster.seats", app)
        self.assertIn("setAgentVisibility", app)
        self.assertIn("restoreAgent", app)
        for layer_id in (
            "metadata_classification",
            "advisory_recommendation",
            "host_selection",
            "actual_invocation",
        ):
            self.assertIn(layer_id, app)
        self.assertIn("managed-seat-list", page)
        self.assertIn("无可信签名宿主回执时只能显示", page)
        self.assertIn("受控唤醒意图（宿主未安装）", page)
        self.assertIn("wakeupSeatLabel", app)
        self.assertIn("建席不会自动开启唤醒", page)


if __name__ == "__main__":
    unittest.main()
