#!/usr/bin/env python3
"""Parity tests for routing JSON Schemas and Python governance contracts."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import routing_gate
import skill_router


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_ROOT = ROOT / "schemas"
SHA256 = "sha256:" + "1" * 64


def routing_metadata() -> dict:
    return {
        "schema_version": skill_router.METADATA_SCHEMA_VERSION,
        "skill": "legal-review",
        "routing_metadata_version": 1,
        "primary_domain": "legal",
        "capabilities": ["review"],
        "triggers": ["合同审查"],
        "negative_triggers": ["不要法律分析"],
        "input_types": ["user_prompt"],
        "output_types": ["task_result"],
        "dependencies": [],
        "risk_tier": "high",
        "allowed_actions": ["advisory_selection"],
        "prohibited_actions": ["automatic_execution"],
        "clarify_when": ["授权范围不明确"],
        "fallback_mode": "legacy_catalog",
        "classification": {
            "method": "human_reviewed",
            "status": "candidate",
            "evidence": ["synthetic-fixture"],
        },
    }


class RoutingSchemaParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        self.skills_root = self.temp_root / "skills"
        skill_root = self.skills_root / "legal-review"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: legal-review\ndescription: synthetic\n---\n",
            encoding="utf-8",
        )
        (skill_root / "routing.json").write_text(
            json.dumps(routing_metadata(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.taxonomy = skill_router.load_taxonomy()
        self.index = skill_router.build_index(
            [("test", self.skills_root)],
            [],
            self.taxonomy,
            mode="shadow",
            router_sha256=skill_router.sha256(Path(skill_router.__file__).read_bytes()),
        )
        self.fixture = {
            "fixture_id": "synthetic-one",
            "sample_source": "synthetic_fixture",
            "authorization_evidence": [],
            "cases": [
                {
                    "id": "case-1",
                    "prompt": "请做合同审查",
                    "case_type": "single_domain",
                    "expected_decision": "recommend",
                    "expected_top": "test:legal-review",
                    "expected_safety_gate": True,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def schema(self, name: str) -> dict:
        return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))

    def errors(self, name: str, instance: object) -> list[str]:
        validator = Draft202012Validator(
            self.schema(name), format_checker=FormatChecker()
        )
        return [error.message for error in validator.iter_errors(instance)]

    def assert_schema_valid(self, name: str, instance: object) -> None:
        self.assertEqual(self.errors(name, instance), [])

    def generated_artifacts(self) -> tuple[dict, dict, dict, dict]:
        plan = skill_router.redacted_fixture_plan(self.index, self.fixture)
        report = skill_router.evaluate_fixture(
            self.index,
            self.taxonomy,
            self.fixture,
            runtime_roots=[("test", self.skills_root)],
        )
        route = skill_router.route_prompt(
            self.fixture["cases"][0]["prompt"],
            self.index,
            self.taxonomy,
            runtime_roots=[("test", self.skills_root)],
        )
        key = self.temp_root / "attestation.key"
        key.write_bytes(b"k" * 32)
        os.chmod(key, 0o600)
        event = skill_router.shadow_event(
            route,
            self.index,
            request_id="case-1",
            baseline_skill="legal-review",
            latency_ms=1.25,
            evaluation_cohort="single_domain",
            prompt_fingerprint=skill_router.prompt_fingerprint(
                self.fixture["cases"][0]["prompt"], key
            ),
            deterministic_replay_verified=True,
        )
        log = self.temp_root / "observations.jsonl"
        signed_event = skill_router.append_observation(log, key, event)
        verification = skill_router.verify_observation_log(
            log, key, key_id="routing-test-key"
        )
        return plan, report, signed_event, verification

    def test_all_routing_schemas_are_valid_draft_2020_12(self) -> None:
        for path in sorted(SCHEMA_ROOT.glob("routing-*.json")) + [
            SCHEMA_ROOT / "manifest-v3.json"
        ]:
            with self.subTest(path=path.name):
                Draft202012Validator.check_schema(
                    json.loads(path.read_text(encoding="utf-8"))
                )

    def test_python_generated_plan_report_event_and_verification_validate(self) -> None:
        plan, report, event, verification = self.generated_artifacts()
        known_runtime_ids = {entry["runtime_id"] for entry in self.index["entries"]}
        self.assertEqual(
            skill_router.validate_fixture_plan(
                plan, known_runtime_ids=known_runtime_ids
            ),
            [],
        )
        self.assertEqual(skill_router.validate_fixture_report(report), [])
        self.assertEqual(skill_router.observation_event_errors(event, signed=True), [])
        self.assert_schema_valid("routing-fixture-plan-v1.json", plan)
        self.assert_schema_valid("routing-fixture-report-v1.json", report)
        self.assert_schema_valid("routing-observation-v1.json", event)
        self.assert_schema_valid(
            "routing-observation-verification-v1.json", verification
        )

    def test_schema_required_key_sets_track_python_contract_constants(self) -> None:
        observation = self.schema("routing-observation-v1.json")
        self.assertEqual(
            set(observation["required"]),
            skill_router.OBSERVATION_BASE_KEYS
            | {"previous_event_sha256", "event_sha256", "hmac_sha256"},
        )
        verification = self.schema("routing-observation-verification-v1.json")
        self.assertEqual(set(verification["required"]), routing_gate.ATTESTATION_KEYS)
        rollback = self.schema("routing-rollback-receipt-v1.json")
        self.assertEqual(set(rollback["required"]), routing_gate.ROLLBACK_RECEIPT_KEYS)
        authorization = self.schema("routing-authorization-receipt-v1.json")
        self.assertEqual(
            set(authorization["required"]), routing_gate.AUTHORIZATION_RECEIPT_KEYS
        )
        fallback = self.schema("routing-fallback-execution-receipt-v1.json")
        self.assertEqual(set(fallback["required"]), routing_gate.FALLBACK_RECEIPT_KEYS)
        self.assertEqual(
            set(fallback["properties"]), routing_gate.FALLBACK_RECEIPT_KEYS
        )
        fallback_case = fallback["$defs"]["fallback_case"]
        self.assertEqual(
            set(fallback_case["required"]), routing_gate.FALLBACK_CASE_KEYS
        )
        self.assertEqual(
            set(fallback_case["properties"]), routing_gate.FALLBACK_CASE_KEYS
        )
        manifest = self.schema("manifest-v3.json")
        shadow = manifest["$defs"]["routing_shadow"]
        self.assertEqual(set(shadow["properties"]), routing_gate.ROUTING_SHADOW_KEYS)
        review = manifest["$defs"]["routing_independent_review"]
        self.assertEqual(
            set(review["required"]), routing_gate.INDEPENDENT_REVIEW_KEYS
        )
        report = self.schema("routing-fixture-report-v1.json")
        self.assertEqual(
            set(report["$defs"]["metrics"]["required"]),
            skill_router.ROUTING_METRIC_KEYS,
        )

    def test_privacy_contracts_reject_raw_or_unknown_fields(self) -> None:
        plan, report, event, verification = self.generated_artifacts()
        plan["cases"][0]["prompt"] = "不得进入计划的案件原文"
        report["results"][0]["prompt"] = "不得进入报告的案件原文"
        event["raw_prompt"] = "不得进入日志的案件原文"
        verification["notes"] = "unknown"
        self.assertTrue(self.errors("routing-fixture-plan-v1.json", plan))
        self.assertTrue(self.errors("routing-fixture-report-v1.json", report))
        self.assertTrue(self.errors("routing-observation-v1.json", event))
        self.assertTrue(
            self.errors("routing-observation-verification-v1.json", verification)
        )
        self.assertTrue(skill_router.validate_fixture_report(report))
        self.assertTrue(skill_router.observation_event_errors(event, signed=True))

    def test_plan_schema_and_python_validator_reject_invalid_expectations(self) -> None:
        plan = skill_router.redacted_fixture_plan(self.index, self.fixture)
        plan["cases"][0]["expected_top"] = "sensitive:case-text"
        base = {key: value for key, value in plan.items() if key != "content_sha256"}
        plan["content_sha256"] = skill_router.sha256(base)
        self.assert_schema_valid("routing-fixture-plan-v1.json", plan)
        errors = skill_router.validate_fixture_plan(
            plan,
            known_runtime_ids={entry["runtime_id"] for entry in self.index["entries"]},
        )
        self.assertTrue(any("outside the bound index" in error for error in errors))

    def test_receipt_schemas_are_strict(self) -> None:
        rollback = {
            "schema_version": "skill-sublation.routing-rollback-receipt.v1",
            "candidate_id": "skill-sublation/candidate",
            "registry_root_id": "registry-test",
            "registry_path": "/private/registry",
            "index_relative_path": "index.json",
            "pre_tree_sha256": SHA256,
            "post_shadow_tree_sha256": "sha256:" + "2" * 64,
            "post_rollback_tree_sha256": SHA256,
            "index_before_sha256": SHA256,
            "index_shadow_sha256": "sha256:" + "2" * 64,
            "index_after_sha256": SHA256,
            "action": "replace_then_restore",
            "performed_at": "2026-08-23T15:00:00Z",
            "index_sha256": "sha256:" + "2" * 64,
            "router_sha256": "sha256:" + "3" * 64,
            "profile_set_sha256": "sha256:" + "4" * 64,
            "fallback_verified": True,
            "registry_rollback_verified": True,
            "legacy_catalog_unchanged": True,
        }
        authorization = {
            "schema_version": 1,
            "adapter_id": "local-test",
            "channel": "four-party-room",
            "event_id": "authorization-event",
            "sender_id": "用户",
            "in_reply_to": "routing-request",
            "message": "批准 shadow 观察",
            "received_at": "2026-08-23T15:00:00Z",
            "source_event_hash": SHA256,
            "report_version": "routing-shadow-v1",
            "report_hash": SHA256,
            "report_body_hash": SHA256,
            "scope_revision": "1",
            "approval_code": "ROUTING-SHADOW-TEST",
            "purpose": "routing_shadow_observation",
            "decision": "approve",
            "candidate_id": "skill-sublation/candidate",
            "candidate_tree_hash": SHA256,
            "promotion_event_hash": SHA256,
            "index_sha256": SHA256,
            "fixture_plan_sha256": SHA256,
            "case_id_set_sha256": SHA256,
            "attestation_key_id": "routing-test-key",
            "attestation_key_sha256": SHA256,
            "mode": "shadow",
            "attestation_mac": "hmac-sha256:" + "5" * 64,
        }
        fallback = {
            "schema_version": "skill-sublation.routing-fallback-execution-receipt.v1",
            "candidate_id": "skill-sublation/candidate",
            "host_adapter_id": "synthetic-host-adapter",
            "host_adapter_sha256": SHA256,
            "index_sha256": SHA256,
            "case_id_set_sha256": SHA256,
            "fallback_case_id_set_sha256": SHA256,
            "fallback_case_count": 1,
            "fallback_requested_count": 1,
            "fallback_executed_count": 1,
            "baseline_outcome_recorded_count": 1,
            "cases": [
                {
                    "case_id": "fallback-case-1",
                    "router_decision": "fallback",
                    "baseline_selection_id": "legacy-catalog-result",
                    "outcome": "completed",
                }
            ],
            "performed_at": "2026-08-23T15:00:00Z",
            "execution_verified": True,
            "legacy_catalog_unchanged": True,
        }
        self.assert_schema_valid("routing-rollback-receipt-v1.json", rollback)
        self.assert_schema_valid(
            "routing-authorization-receipt-v1.json", authorization
        )
        self.assert_schema_valid(
            "routing-fallback-execution-receipt-v1.json", fallback
        )
        for name, value in (
            ("routing-rollback-receipt-v1.json", rollback),
            ("routing-authorization-receipt-v1.json", authorization),
            ("routing-fallback-execution-receipt-v1.json", fallback),
        ):
            mutated = copy.deepcopy(value)
            mutated["unexpected"] = True
            self.assertTrue(self.errors(name, mutated))
        for key, invalid in (
            ("fallback_case_count", 0),
            ("host_adapter_sha256", "sha256:not-a-digest"),
            ("performed_at", "2026-08-23T15:00:00"),
            ("execution_verified", False),
            ("legacy_catalog_unchanged", False),
        ):
            mutated = copy.deepcopy(fallback)
            mutated[key] = invalid
            self.assertTrue(
                self.errors("routing-fallback-execution-receipt-v1.json", mutated),
                key,
            )
        mutated = copy.deepcopy(fallback)
        mutated["cases"][0]["raw_prompt"] = "不得进入收据的原文"
        self.assertTrue(
            self.errors("routing-fallback-execution-receipt-v1.json", mutated)
        )

    def manifest(self, shadow: dict) -> dict:
        return {
            "schema_version": 3,
            "candidate_id": "skill-sublation/candidate",
            "candidate_type": "tooling",
            "source_skill": {"path": "/source", "tree_hash": SHA256, "files": {}},
            "candidate": {"path": "/candidate", "tree_hash": SHA256, "files": {}},
            "scope": {"changes": [], "out_of_scope": []},
            "routing": {
                "required_on_promotion": True,
                "metadata_path": "routing.json",
                "metadata_sha256": SHA256,
                "classification_status": "candidate",
                "index_mode": "shadow",
                "tree_hash_algorithm": "sublation-canonical-tree-v1",
                "attestation_key_id": None,
                "attestation_key_sha256": None,
            },
            "validation": {
                "auditor_status": "passed",
                "cross_reviewed_by": "none",
                "promotion_mode": "none",
                "status": "review_pending",
                "routing_shadow": shadow,
            },
        }

    def metric_evidence(self) -> dict:
        return {
            "explicit_invocation_adherence": 1.0,
            "deterministic_order_rate": 1.0,
            "fallback_success_rate": 1.0,
            "negative_control_misroutes": 0,
            "high_risk_misroutes": 0,
            "authority_expansion_events": 0,
            "raw_prompt_leaks": 0,
            "tree_drift_misses": 0,
            "valid_samples": 30,
            "single_domain_top1": 1.0,
            "multi_domain_top3": 1.0,
            "route_accuracy": 1.0,
            "baseline_route_accuracy": 1.0,
            "manual_correction_rate": 0.0,
            "baseline_manual_correction_rate": 0.0,
            "p95_router_latency_ms": 1.0,
            "net_token_savings_status": "not_measurable",
            "host_deferred_loading_verified": False,
            "fallback_verified": True,
            "registry_rollback_verified": False,
        }

    def offline_shadow(self) -> dict:
        return {
            "status": "offline_passed",
            "mode": "shadow",
            "sample_source": "synthetic_fixture",
            "boundary": "离线夹具仅证明候选能力，不构成运行时观察闭环",
            "index_path": "validation/index.json",
            "index_sha256": SHA256,
            "router_sha256": SHA256,
            "profile_set_sha256": SHA256,
            "fixture_report_path": "validation/report.json",
            "fixture_report_sha256": SHA256,
            "fixture_plan_path": "validation/plan.json",
            "fixture_plan_sha256": SHA256,
            "fixture_plan_artifact_sha256": SHA256,
            **self.metric_evidence(),
        }

    def test_manifest_distinguishes_offline_observing_and_passed_shadow(self) -> None:
        offline = self.offline_shadow()
        self.assert_schema_valid("manifest-v3.json", self.manifest(offline))
        offline["observation_log_path"] = "validation/observations.jsonl"
        self.assertTrue(self.errors("manifest-v3.json", self.manifest(offline)))
        offline = self.offline_shadow()
        offline["host_adapter_id"] = "synthetic-host-adapter"
        self.assertTrue(self.errors("manifest-v3.json", self.manifest(offline)))

        observing = {
            "status": "observing",
            "mode": "shadow",
            "sample_source": "authorized_shadow_runtime",
            "boundary": "已授权 shadow 观察，不启用 live 路由",
        }
        self.assertTrue(self.errors("manifest-v3.json", self.manifest(observing)))
        observing.update(
            {
                "attestation_key_id": "routing-test-key",
                "attestation_key_sha256": SHA256,
                "index_path": "validation/index.json",
                "index_sha256": SHA256,
                "router_sha256": SHA256,
                "profile_set_sha256": SHA256,
                "fixture_plan_path": "validation/plan.json",
                "fixture_plan_sha256": SHA256,
                "fixture_plan_artifact_sha256": SHA256,
                "authorization_run_dir": "/private/run",
                "authorization_receipt_path": "/private/run/receipt.json",
                "authorization_receipt_sha256": SHA256,
                "promotion_event_hash": SHA256,
            }
        )
        self.assert_schema_valid("manifest-v3.json", self.manifest(observing))
        observing["fallback_receipt_path"] = "/private/run/fallback.json"
        self.assertTrue(self.errors("manifest-v3.json", self.manifest(observing)))
        del observing["fallback_receipt_path"]

        passed = {
            **observing,
            "status": "passed",
            "fixture_report_path": "validation/report.json",
            "fixture_report_sha256": SHA256,
            "observation_log_path": "validation/observations.jsonl",
            "observation_log_sha256": SHA256,
            "observation_event_count": 30,
            "attestation_report_path": "validation/attestation.json",
            "attestation_report_sha256": SHA256,
            "host_adapter_id": "synthetic-host-adapter",
            "host_adapter_path": "/private/synthetic-host-adapter.py",
            "host_adapter_sha256": SHA256,
            "fallback_receipt_path": "/private/run/fallback.json",
            "fallback_receipt_sha256": SHA256,
            "fallback_event_hash": SHA256,
            "rollback_receipt_path": "/private/run/rollback.json",
            "rollback_receipt_sha256": SHA256,
            "rollback_event_hash": SHA256,
            "independent_reviews": [
                {
                    "reviewer": reviewer,
                    "message_id": f"review-{reviewer}",
                    "decision": "approve",
                    "room_log_path": "/private/review-room.jsonl",
                    "message_sha256": SHA256,
                    "artifact_binding_sha256": SHA256,
                }
                for reviewer in ("hermes", "claude-code")
            ],
            **{**self.metric_evidence(), "registry_rollback_verified": True},
        }
        self.assert_schema_valid("manifest-v3.json", self.manifest(passed))
        self_reported_fallback = copy.deepcopy(passed)
        for key in (
            "host_adapter_id",
            "host_adapter_path",
            "host_adapter_sha256",
            "fallback_receipt_path",
            "fallback_receipt_sha256",
            "fallback_event_hash",
        ):
            del self_reported_fallback[key]
        self.assertTrue(self_reported_fallback["fallback_verified"])
        self.assertTrue(
            self.errors("manifest-v3.json", self.manifest(self_reported_fallback))
        )
        del passed["rollback_event_hash"]
        self.assertTrue(self.errors("manifest-v3.json", self.manifest(passed)))

    def test_manifest_routing_block_rejects_unknown_or_unpaired_key_fields(self) -> None:
        offline = self.offline_shadow()
        manifest = self.manifest(offline)
        manifest["routing"]["unexpected"] = True
        self.assertTrue(self.errors("manifest-v3.json", manifest))
        manifest = self.manifest(offline)
        manifest["routing"]["attestation_key_id"] = "routing-key"
        self.assertTrue(self.errors("manifest-v3.json", manifest))


if __name__ == "__main__":
    unittest.main()
