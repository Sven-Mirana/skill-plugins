#!/usr/bin/env python3
"""Contract tests for the Sublation V5.0 panel and routing boundary."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
SHA256 = "sha256:" + "1" * 64


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected object: {path}")
    return value


class V5ContractTests(unittest.TestCase):
    def schema(self, name: str) -> dict:
        return load_json(SCHEMAS / name)

    def errors(self, name: str, value: object) -> list[str]:
        validator = Draft202012Validator(
            self.schema(name), format_checker=FormatChecker()
        )
        return [error.message for error in validator.iter_errors(value)]

    def roster(self) -> dict:
        return {
            "schema_version": "skill-sublation.collaboration-roster.v1",
            "project_id": "demo-project",
            "revision": 1,
            "max_agent_seats": 12,
            "history_mode": "append_only",
            "seat_changes_grant_authority": False,
            "updated_at": "2026-08-24T00:00:00Z",
            "updated_by": "user",
            "seats": [
                {
                    "id": "user",
                    "kind": "user",
                    "display_name": "root",
                    "role_label": "决策与输入",
                    "active": True,
                    "visible": True,
                    "receives_broadcast": True,
                    "can_own_tasks": False,
                    "participation_mode": "local_user",
                    "adapter_id": "local-user",
                    "joined_at": "2026-08-24T00:00:00Z",
                    "retired_at": None,
                },
                {
                    "id": "codex",
                    "kind": "agent",
                    "display_name": "Codex",
                    "role_label": "构建与执行",
                    "active": True,
                    "visible": True,
                    "receives_broadcast": True,
                    "can_own_tasks": True,
                    "participation_mode": "task_polled",
                    "adapter_id": "codex-task-poll",
                    "joined_at": "2026-08-24T00:00:00Z",
                    "retired_at": None,
                },
            ],
        }

    def routing_snapshot(self) -> dict:
        return {
            "schema_version": "skill-sublation.collaboration-routing-snapshot.v1",
            "request_id": "demo-1",
            "metadata_classification": {
                "domain": "meta-governance",
                "capabilities": ["governance"],
                "status": "candidate",
            },
            "advisory_recommendation": {
                "mode": "shadow",
                "decision": "recommend",
                "skills": ["skill-sublation"],
                "safety_gate_required": True,
            },
            "host_selection": {
                "seat_id": "codex",
                "baseline": ["legacy-choice"],
                "effective": ["legacy-choice"],
                "preserved": True,
            },
            "actual_invocation": {
                "status": "not_performed",
                "receipt_sha256": None,
            },
            "raw_prompt_stored": False,
            "authority_granted_by_router": False,
        }

    def test_v5_version_is_single_and_candidate_bounded(self) -> None:
        version = load_json(ROOT / "version.json")
        self.assertEqual(version["version"], "5.0.0")
        self.assertEqual(version["release_channel"], "governed-local")
        self.assertEqual(
            version["lifecycle_state_source"],
            "candidate-manifest-or-install-receipt",
        )
        self.assertIn("automatic-skill-invocation", version["prohibited_claims"])
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("# Skill Sublation v5.0", skill_text)
        self.assertNotIn("# Skill Sublation v2.0", skill_text)

    def test_new_schemas_are_valid_draft_2020_12(self) -> None:
        for name in (
            "collaboration-roster-v1.json",
            "collaboration-roster-event-v1.json",
            "collaboration-routing-snapshot-v1.json",
            "routing-invocation-receipt-v1.json",
            "agent-wakeup-policy-v1.json",
            "agent-wakeup-receipt-v1.json",
        ):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(self.schema(name))

    def test_wakeup_policy_is_default_off_and_rejects_command_fields(self) -> None:
        policy = load_json(ROOT / "fixtures" / "collaboration-panel" / "wakeup-policies.json")
        self.assertEqual(self.errors("agent-wakeup-policy-v1.json", policy), [])
        self.assertFalse(policy["default_policy"]["enabled"])
        self.assertEqual(policy["default_policy"]["mode"], "queue_only")
        self.assertTrue(policy["window_policy"]["requires_explicit_user_arm"])
        forged = copy.deepcopy(policy)
        forged["registered_adapters"] = [
            {
                "adapter_id": "synthetic-test-adapter",
                "adapter_sha256": SHA256,
                "kind": "task_poll",
                "allowed_seat_ids": ["codex"],
            }
        ]
        forged["registered_adapters"][0]["argv"] = ["claude", "-p"]
        self.assertTrue(self.errors("agent-wakeup-policy-v1.json", forged))

    def test_wakeup_receipt_requires_verified_delivery_and_no_secret_or_command_flags(self) -> None:
        receipt = {
            "schema_version": "skill-sublation.agent-wakeup-receipt.v1",
            "receipt_id": "wake-receipt-demo",
            "project_id": "project-demo",
            "project_root": "/tmp/project-demo",
            "room_identity": SHA256,
            "intent_id": "wake-intent-demo",
            "window_id": "wake-window-demo",
            "policy_revision": 1,
            "policy_sha256": SHA256,
            "claim_id": "wake-claim-demo",
            "nonce": "2" * 64,
            "key_id": "wakeup-key-demo",
            "seat_id": "claude-code",
            "seat_generation": SHA256,
            "source_message_id": "message-demo",
            "idempotency_key": SHA256,
            "adapter_id": "claude-code-watcher-auto-v1",
            "adapter_sha256": SHA256,
            "status": "sent_and_verified",
            "sent_at": "2026-08-24T00:00:00Z",
            "verified_at": "2026-08-24T00:00:01Z",
            "delivery_ref": "message-reply-demo",
            "failure_code": None,
            "external_command_accepted": False,
            "secrets_stored": False,
            "receipt_sha256": SHA256,
            "receipt_hmac": "hmac-sha256:" + "3" * 64,
        }
        self.assertEqual(self.errors("agent-wakeup-receipt-v1.json", receipt), [])
        missing_delivery = copy.deepcopy(receipt)
        missing_delivery["delivery_ref"] = None
        self.assertTrue(self.errors("agent-wakeup-receipt-v1.json", missing_delivery))
        unsafe = copy.deepcopy(receipt)
        unsafe["external_command_accepted"] = True
        self.assertTrue(self.errors("agent-wakeup-receipt-v1.json", unsafe))
        failed = copy.deepcopy(receipt)
        failed.update(
            {
                "status": "failed",
                "sent_at": None,
                "verified_at": None,
                "delivery_ref": None,
                "failure_code": "adapter_failed",
            }
        )
        self.assertEqual(self.errors("agent-wakeup-receipt-v1.json", failed), [])
        failed_with_delivery = copy.deepcopy(failed)
        failed_with_delivery["delivery_ref"] = "must-not-look-delivered"
        self.assertTrue(self.errors("agent-wakeup-receipt-v1.json", failed_with_delivery))

    def test_roster_contract_is_strict_and_never_grants_authority(self) -> None:
        roster = self.roster()
        self.assertEqual(self.errors("collaboration-roster-v1.json", roster), [])
        changed = copy.deepcopy(roster)
        changed["seat_changes_grant_authority"] = True
        self.assertTrue(self.errors("collaboration-roster-v1.json", changed))
        changed = copy.deepcopy(roster)
        changed["seats"][1]["adapter_command"] = ["unsafe"]
        self.assertTrue(self.errors("collaboration-roster-v1.json", changed))

    def test_routing_snapshot_cannot_claim_invocation_without_receipt(self) -> None:
        snapshot = self.routing_snapshot()
        self.assertEqual(
            self.errors("collaboration-routing-snapshot-v1.json", snapshot), []
        )
        forged = copy.deepcopy(snapshot)
        forged["actual_invocation"]["status"] = "completed"
        self.assertTrue(
            self.errors("collaboration-routing-snapshot-v1.json", forged)
        )

    def test_routing_snapshot_and_receipt_reject_raw_prompt_fields(self) -> None:
        snapshot = self.routing_snapshot()
        snapshot["prompt"] = "不得持久化的输入"
        self.assertTrue(
            self.errors("collaboration-routing-snapshot-v1.json", snapshot)
        )
        receipt = {
            "schema_version": "skill-sublation.routing-invocation-receipt.v1",
            "request_id": "demo-1",
            "host_id": "codex",
            "executing_agent_id": "codex",
            "routing_mode": "host_selected",
            "recommended_skill_id": "skill-sublation",
            "host_selected_skill_id": "skill-sublation",
            "invoked_skill_id": "skill-sublation",
            "invocation_status": "completed",
            "started_at": "2026-08-24T00:00:00Z",
            "finished_at": "2026-08-24T00:00:01Z",
            "host_adapter_sha256": SHA256,
            "active_catalog_sha256": SHA256,
            "result_receipt_sha256": SHA256,
            "raw_prompt_stored": False,
            "authority_granted_by_router": False,
        }
        self.assertEqual(
            self.errors("routing-invocation-receipt-v1.json", receipt), []
        )
        receipt["raw_prompt"] = "不得进入收据"
        self.assertTrue(
            self.errors("routing-invocation-receipt-v1.json", receipt)
        )


if __name__ == "__main__":
    unittest.main()
