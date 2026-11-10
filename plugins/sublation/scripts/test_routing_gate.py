#!/usr/bin/env python3
"""Tests for routing metadata, promotion, and observation closure gates."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import routing_gate
import skill_router
import sublation_promote
import sublation_receipt
import sublation_run


def routing_metadata(*, status: str = "candidate") -> dict:
    return {
        "schema_version": skill_router.METADATA_SCHEMA_VERSION,
        "skill": "synthetic-skill",
        "routing_metadata_version": 1,
        "primary_domain": "meta-governance",
        "capabilities": ["governance", "validation"],
        "triggers": ["候选治理", "路由验证"],
        "negative_triggers": ["绕过审批"],
        "input_types": ["user_prompt"],
        "output_types": ["candidate_report"],
        "dependencies": [],
        "risk_tier": "high",
        "allowed_actions": ["shadow_routing"],
        "prohibited_actions": ["automatic_promotion"],
        "clarify_when": ["授权范围不明确"],
        "fallback_mode": "legacy_catalog",
        "classification": {
            "method": "human_reviewed",
            "status": status,
            "evidence": (
                ["review-hermes", "review-claude"]
                if status == "approved"
                else ["synthetic-test"]
            ),
        },
    }


def manifest_for(metadata: dict, *, validation_status: str = "review_pending") -> dict:
    manifest = {
        "schema_version": 3,
        "candidate_id": "synthetic/candidate",
        "created_at": "2026-08-23T14:29:35Z",
        "created_by": "codex",
        "routing": {
            "required_on_promotion": True,
            "metadata_path": "routing.json",
            "metadata_sha256": skill_router.metadata_sha256(metadata),
            "classification_status": metadata["classification"]["status"],
            "index_mode": "shadow",
            "tree_hash_algorithm": skill_router.TREE_HASH_ALGORITHM,
            "attestation_key_id": None,
            "attestation_key_sha256": None,
        },
        "validation": {
            "status": validation_status,
            "promoted_at": "2026-08-23T14:29:35Z",
        },
    }
    if metadata["classification"]["status"] == "approved":
        manifest["validation"]["pre_promotion_reports"] = [
            {
                "reviewer": "hermes",
                "status": "approve",
                "focus": "boundary",
                "message_id": "review-hermes",
            },
            {
                "reviewer": "claude-code",
                "status": "approve",
                "focus": "implementation",
                "message_id": "review-claude",
            },
        ]
    return manifest


class RoutingGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "candidate"
        self.root.mkdir()
        self.private_root = Path(self.temporary.name) / "private"
        self.private_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_json(self, relative: str, value: dict, *, mode: int | None = None) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if mode is not None:
            os.chmod(path, mode)
        return path

    def write_metadata(self, value: dict) -> None:
        self.write_json("routing.json", value)

    def execute_real_promotion(self, manifest: dict) -> tuple[Path, dict]:
        formal = self.private_root / "formal" / "synthetic-skill"
        formal.mkdir(parents=True)
        (formal / "SKILL.md").write_text("before\n", encoding="utf-8")
        patch = """diff --git a/SKILL.md b/SKILL.md
--- a/SKILL.md
+++ b/SKILL.md
@@ -1 +1,2 @@
 before
+after
"""
        (self.root / "PATCH.diff").write_text(patch, encoding="utf-8")
        self.write_json("manifest.json", manifest)
        run_dir = sublation_run.start_run(
            "把 synthetic skill 做 Sublation 候选晋升测试",
            self.private_root / "runs",
            [{"name": "synthetic", "path": str(formal)}],
            "routing-auth-test",
        )
        identities = {
            "codex": {
                "principal_id": "codex-routing-test",
                "adapter_fingerprint": "sha256:codex-routing-test",
            },
            "claude-code": {
                "principal_id": "claude-routing-test",
                "adapter_fingerprint": "sha256:claude-routing-test",
            },
            "hermes": {
                "principal_id": "hermes-routing-test",
                "adapter_fingerprint": "sha256:hermes-routing-test",
            },
        }
        sublation_run.bind_worker_identities(
            run_dir,
            {
                actor: {
                    **identity,
                    "write_roots": [str(self.root.parent)] if actor == "codex" else [],
                }
                for actor, identity in identities.items()
            },
        )
        candidate_tree_hash = sublation_run.tree_hash(self.root)
        phases = (
            ("codex", "observe", "OBSERVED"),
            ("codex", "candidate", "CANDIDATE_READY"),
            ("codex", "audit", "AUDIT_PASSED"),
            ("claude-code", "independent_verify", "VERIFY_PASSED"),
            ("hermes", "boundary_review", "REVIEW_PASSED"),
        )
        for position, (actor, phase, status) in enumerate(phases, start=1):
            task = sublation_run.claim_task(run_dir, actor, 120, "A1")
            assert task is not None
            evidence_path = run_dir / "evidence" / f"phase-{position}.json"
            sublation_run.atomic_write_json(evidence_path, {"phase": phase})
            sublation_run.record_step(
                run_dir,
                step_id=f"phase-{position}",
                item_id="A1",
                actor=actor,
                phase=phase,
                step_status="pass",
                item_status=status,
                candidate_path=(str(self.root) if status == "CANDIDATE_READY" else None),
                evidence=[str(evidence_path)],
                task_id=task["task_id"],
                lease_token=task["lease"]["token"],
                executor_principal=identities[actor]["principal_id"],
                adapter_fingerprint=identities[actor]["adapter_fingerprint"],
                candidate_tree_hash=(candidate_tree_hash if phase != "observe" else None),
            )
        final_task = sublation_run.claim_task(run_dir, "hermes", 120, "A1")
        assert final_task is not None
        final_evidence = run_dir / "evidence" / "approval-ready.json"
        sublation_run.atomic_write_json(final_evidence, {"status": "approval-ready"})
        sublation_run.record_step(
            run_dir,
            step_id="approval-ready",
            item_id="A1",
            actor="hermes",
            phase="aggregate",
            step_status="pass",
            item_status="APPROVAL_READY",
            candidate_id=manifest["candidate_id"],
            disposition="promotion",
            summary="Promote the isolated routing test patch.",
            evidence=[str(final_evidence)],
            task_id=final_task["task_id"],
            lease_token=final_task["lease"]["token"],
            executor_principal=identities["hermes"]["principal_id"],
            adapter_fingerprint=identities["hermes"]["adapter_fingerprint"],
            candidate_tree_hash=candidate_tree_hash,
        )
        promotion_report = sublation_run.finalize_report(run_dir)
        report_body_hash = promotion_report["plain_report_sha256"]
        delivery_text_hash = sublation_run.sha256(
            f"[SUBLATION FINAL REPORT routing-test]\n{report_body_hash}"
        )
        delivery_evidence = run_dir / "evidence" / "delivery.json"
        sublation_run.atomic_write_json(
            delivery_evidence,
            {
                "message_ref": "routing-promotion-report",
                "sender_actor": "hermes",
                "report_body_hash": report_body_hash,
                "delivery_text_hash": delivery_text_hash,
            },
        )
        sublation_run.record_delivery(
            run_dir,
            "four-party-room",
            "routing-promotion-report",
            ["用户"],
            sender_actor="hermes",
            idempotency_key="routing-promotion-report",
            adapter_evidence_path=str(delivery_evidence),
            report_body_hash=report_body_hash,
            delivery_text_hash=delivery_text_hash,
        )
        approval_receipt = sublation_run.write_receipt_attestation(
            run_dir,
            {
                "adapter_id": "local-routing-test",
                "channel": "four-party-room",
                "event_id": "routing-promotion-approval",
                "sender_id": "用户",
                "in_reply_to": "routing-promotion-report",
                "message": "准A1",
                "received_at": sublation_run.utc_now(),
                "source_event_hash": sublation_run.sha256(b"routing-promotion-approval"),
                "report_version": promotion_report["report_version"],
                "report_hash": promotion_report["report_hash"],
                "report_body_hash": report_body_hash,
                "scope_revision": promotion_report["scope_revision"],
                "approval_code": promotion_report["approval_code"],
            },
        )
        sublation_receipt.apply_receipt(
            run_dir, receipt_evidence_path=approval_receipt
        )
        sublation_promote.execute_approved(
            run_dir,
            allowed_targets={"A1": formal.resolve()},
            rollback_root=self.private_root / "rollbacks",
        )
        promotion_events = [
            event
            for event in sublation_run.verified_journal_entries(run_dir)
            if event.get("type") == "promotion_succeeded"
        ]
        self.assertEqual(len(promotion_events), 1)
        return run_dir, promotion_events[0]

    def complete_shadow(self, manifest: dict) -> dict:
        metadata = routing_metadata(status="approved")
        self.write_metadata(metadata)
        profiles = self.root / "profiles" / "synthetic-skill"
        profiles.mkdir(parents=True)
        (profiles / "SKILL.md").write_text(
            "---\nname: synthetic-skill\ndescription: synthetic\n---\n",
            encoding="utf-8",
        )
        (profiles / "routing.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        secondary = routing_metadata(status="approved")
        secondary.update(
            {
                "skill": "secondary-skill",
                "primary_domain": "finance-investment",
                "capabilities": ["research", "validation"],
                "triggers": ["财务测算"],
                "negative_triggers": ["不要财务分析"],
            }
        )
        secondary_root = self.root / "profiles" / "secondary-skill"
        secondary_root.mkdir()
        (secondary_root / "SKILL.md").write_text(
            "---\nname: secondary-skill\ndescription: synthetic\n---\n",
            encoding="utf-8",
        )
        (secondary_root / "routing.json").write_text(
            json.dumps(secondary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        router_hash = skill_router.sha256(Path(skill_router.__file__).read_bytes())
        index = skill_router.build_index(
            [("fixture", self.root / "profiles")],
            [],
            skill_router.load_taxonomy(),
            mode="shadow",
            router_sha256=router_hash,
        )
        self.write_json("validation/index.json", index)
        key = self.private_root / "attestation.key"
        key.write_bytes(b"k" * 32)
        os.chmod(key, 0o600)
        self.attestation_key = key
        key_id = "routing-test-key"
        key_hash = skill_router.sha256(key.read_bytes())
        manifest["routing"]["attestation_key_id"] = key_id
        manifest["routing"]["attestation_key_sha256"] = key_hash

        cohort_types = (
            ["single_domain"] * 10
            + ["multi_domain"] * 3
            + ["explicit"] * 3
            + ["fallback"] * 3
            + ["negative_control"] * 3
            + ["high_risk"] * 3
            + ["tree_drift"] * 3
            + ["general"] * 2
        )
        case_ids = [f"opaque-{position}" for position in range(30)]
        fixture_plan = []
        results = []
        routes = []
        log = self.root / "validation" / "observations.jsonl"
        for position, case_type in enumerate(cohort_types):
            prompt_hash = skill_router.prompt_fingerprint(
                f"synthetic prompt {case_ids[position]}", key
            )
            recommended_ids = (
                []
                if case_type in {"fallback", "tree_drift"}
                else ["fixture:synthetic-skill", "fixture:secondary-skill"]
                if case_type == "multi_domain"
                else ["fixture:synthetic-skill"]
            )
            decision = (
                "compose_or_clarify"
                if case_type == "multi_domain"
                else "fallback"
                if case_type in {"fallback", "tree_drift"}
                else "recommend"
            )
            reason_codes = (
                ["runtime_binding_invalid"]
                if case_type == "tree_drift"
                else ["low_confidence_or_no_match"]
                if case_type == "fallback"
                else ["cross_domain_candidates"]
                if case_type == "multi_domain"
                else ["high_risk_requires_authority_check"]
                if case_type == "high_risk"
                else []
            )
            route = {
                "index_mode": "shadow",
                "advisory_only": True,
                "authority_granted": False,
                "decision": decision,
                "recommended": [
                    {"runtime_id": runtime_id} for runtime_id in recommended_ids
                ],
                "fallback_used": decision == "fallback",
                "clarification_required": case_type == "multi_domain",
                "safety_gate_required": case_type == "high_risk",
                "reason_codes": reason_codes,
                "runtime_binding_verified": False if case_type == "tree_drift" else True,
            }
            routes.append(route)
            results.append(
                {
                    "case_id": case_ids[position],
                    "case_type": case_type,
                    "passed": True,
                    "failure_codes": [],
                    "decision": decision,
                    "recommended_runtime_ids": recommended_ids,
                    "safety_gate_required": case_type == "high_risk",
                    "fallback_used": decision == "fallback",
                    "clarification_required": case_type == "multi_domain",
                    "reason_codes": reason_codes,
                    "runtime_binding_verified": False if case_type == "tree_drift" else True,
                    "deterministic_replay_verified": True,
                }
            )
            fixture_plan.append(
                {
                    "case_id": case_ids[position],
                    "prompt_fingerprint": prompt_hash,
                    "case_type": case_type,
                    "expected_decision": decision,
                    "expected_top": recommended_ids[0] if recommended_ids else None,
                    "expected_contains": recommended_ids if case_type == "multi_domain" else [],
                    "expected_excludes": (
                        ["fixture:secondary-skill"]
                        if case_type == "negative_control"
                        else []
                    ),
                    "expected_safety_gate": True if case_type == "high_risk" else None,
                }
            )
        metrics = {
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
            "baseline_route_accuracy": 0.90,
            "manual_correction_rate": 0.0,
            "baseline_manual_correction_rate": 0.10,
            "p95_router_latency_ms": 5.0,
            "net_token_savings_status": "not_measurable",
            "host_deferred_loading_verified": True,
            "fallback_verified": True,
            "registry_rollback_verified": True,
        }
        result_projection = [
            {
                key: result[key]
                for key in (
                    "case_id",
                    "case_type",
                    "decision",
                    "recommended_runtime_ids",
                    "fallback_used",
                    "clarification_required",
                    "safety_gate_required",
                    "reason_codes",
                    "runtime_binding_verified",
                    "deterministic_replay_verified",
                )
            }
            for result in results
        ]
        plan_base = {
            "schema_version": "skill-sublation.routing-fixture-plan.v1",
            "fixture_id": "authorized-shadow",
            "sample_source": "authorized_shadow_runtime",
            "index_sha256": index["content_sha256"],
            "prompt_fingerprint_type": "hmac-sha256",
            "cases": sorted(fixture_plan, key=lambda item: item["case_id"]),
        }
        plan_document = {
            **plan_base,
            "content_sha256": skill_router.sha256(plan_base),
        }
        plan_path = self.write_json("validation/plan.json", plan_document)
        fixture_plan_sha256 = plan_document["content_sha256"]
        report = {
            "schema_version": "skill-sublation.routing-fixture-report.v1",
            "fixture_id": "authorized-shadow",
            "sample_source": "authorized_shadow_runtime",
            "authorization_evidence": ["synthetic-authorized-test"],
            "index_sha256": index["content_sha256"],
            "case_id_set_sha256": skill_router.sha256(sorted(case_ids)),
            "fixture_plan_sha256": fixture_plan_sha256,
            "result_projection_sha256": skill_router.sha256(
                sorted(result_projection, key=lambda item: item["case_id"])
            ),
            "total": 30,
            "passed": 30,
            "failed": 0,
            "pass_rate": 1.0,
            "raw_prompt_leaks": 0,
            "metrics": metrics,
            "results": results,
        }
        report_path = self.write_json("validation/report.json", report)

        authorization_run, promotion_event = self.execute_real_promotion(manifest)
        candidate_tree_hash = promotion_event["payload"]["candidate_tree_hash"]
        request_payload = {
            "candidate_id": manifest["candidate_id"],
            "candidate_tree_hash": candidate_tree_hash,
            "promotion_event_hash": promotion_event["event_hash"],
            "index_sha256": index["content_sha256"],
            "fixture_plan_sha256": fixture_plan_sha256,
            "case_id_set_sha256": report["case_id_set_sha256"],
            "attestation_key_id": key_id,
            "attestation_key_sha256": key_hash,
            "mode": "shadow",
            "channel": "four-party-room",
            "message_ref": "routing-shadow-request",
            "report_version": "routing-shadow-v1",
            "report_hash": skill_router.sha256({"request": "routing-shadow"}),
            "report_body_hash": skill_router.sha256(b"routing-shadow-request"),
            "scope_revision": "1",
            "approval_code": "ROUTING-SHADOW-TEST",
            "authorized_reply_senders": ["用户"],
        }
        sublation_run.append_event(
            authorization_run,
            "routing_shadow_authorization_requested",
            request_payload,
        )
        receipt_payload = {
            "adapter_id": "local-test",
            "channel": request_payload["channel"],
            "event_id": "synthetic-authorized-test",
            "sender_id": "用户",
            "in_reply_to": request_payload["message_ref"],
            "message": "批准 routing shadow observation",
            "received_at": sublation_run.utc_now(),
            "source_event_hash": skill_router.sha256(b"synthetic-user-event"),
            "report_version": request_payload["report_version"],
            "report_hash": request_payload["report_hash"],
            "report_body_hash": request_payload["report_body_hash"],
            "scope_revision": request_payload["scope_revision"],
            "approval_code": request_payload["approval_code"],
            "purpose": "routing_shadow_observation",
            "decision": "approve",
            "candidate_id": manifest["candidate_id"],
            "candidate_tree_hash": candidate_tree_hash,
            "promotion_event_hash": promotion_event["event_hash"],
            "index_sha256": index["content_sha256"],
            "fixture_plan_sha256": fixture_plan_sha256,
            "case_id_set_sha256": report["case_id_set_sha256"],
            "attestation_key_id": key_id,
            "attestation_key_sha256": key_hash,
            "mode": "shadow",
        }
        receipt_path = sublation_run.write_receipt_attestation(
            authorization_run, receipt_payload
        )
        receipt, receipt_record = sublation_run.verify_receipt_attestation(
            authorization_run, receipt_path
        )
        sublation_run.append_event(
            authorization_run,
            "approval_receipt_recorded",
            {
                "event_id": receipt["event_id"],
                "event_signature": receipt["attestation_mac"],
                "channel": receipt["channel"],
                "sender_id": receipt["sender_id"],
                "in_reply_to": receipt["in_reply_to"],
                "report_version": receipt["report_version"],
                "report_hash": receipt["report_hash"],
                "report_body_hash": receipt["report_body_hash"],
                "scope_revision": receipt["scope_revision"],
                "receipt_evidence": receipt_record,
                "purpose": "routing_shadow_observation",
                "decision": "approve",
            },
        )

        for position, (case_type, route) in enumerate(zip(cohort_types, routes)):
            event = skill_router.shadow_event(
                route,
                index,
                request_id=case_ids[position],
                baseline_skill="legacy-catalog",
                latency_ms=5.0,
                evaluation_cohort=case_type,
                prompt_fingerprint=fixture_plan[position]["prompt_fingerprint"],
                deterministic_replay_verified=True,
            )
            skill_router.append_observation(log, key, event)
        attestation = skill_router.verify_observation_log(log, key, key_id=key_id)
        self.write_json("validation/attestation.json", attestation)

        host_adapter = self.private_root / "synthetic-host-adapter.py"
        host_adapter.write_text(
            "# synthetic test adapter; no production authority\n",
            encoding="utf-8",
        )
        host_adapter_id = "synthetic-host-adapter"
        host_adapter_sha256 = skill_router.sha256(host_adapter.read_bytes())
        fallback_case_ids = sorted(
            result["case_id"]
            for result in results
            if result["decision"] == "fallback"
        )
        fallback_record = {
            "schema_version": "skill-sublation.routing-fallback-execution-receipt.v1",
            "candidate_id": manifest["candidate_id"],
            "host_adapter_id": host_adapter_id,
            "host_adapter_sha256": host_adapter_sha256,
            "index_sha256": index["content_sha256"],
            "case_id_set_sha256": report["case_id_set_sha256"],
            "fallback_case_id_set_sha256": skill_router.sha256(
                fallback_case_ids
            ),
            "fallback_case_count": len(fallback_case_ids),
            "fallback_requested_count": len(fallback_case_ids),
            "fallback_executed_count": len(fallback_case_ids),
            "baseline_outcome_recorded_count": len(fallback_case_ids),
            "cases": [
                {
                    "case_id": case_id,
                    "router_decision": "fallback",
                    "baseline_selection_id": "legacy-catalog-result",
                    "outcome": "completed",
                }
                for case_id in fallback_case_ids
            ],
            "performed_at": sublation_run.utc_now(),
            "execution_verified": True,
            "legacy_catalog_unchanged": True,
        }
        fallback_path = authorization_run / "routing-fallback-receipt.json"
        sublation_run.atomic_write_json(fallback_path, fallback_record)
        fallback_receipt_sha256 = skill_router.sha256(fallback_path.read_bytes())
        fallback_event = sublation_run.append_event(
            authorization_run,
            "routing_host_fallback_execution_verified",
            {
                **fallback_record,
                "fallback_receipt_sha256": fallback_receipt_sha256,
            },
        )

        registry = self.private_root / "registry"
        registry.mkdir()
        baseline_index = {
            "content_sha256": skill_router.sha256({"legacy": True}),
            "legacy": True,
        }
        registry_index = registry / "index.json"
        registry_index.write_text(
            json.dumps(baseline_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        baseline_bytes = registry_index.read_bytes()
        pre_tree_sha256 = sublation_run.tree_hash(registry)
        registry_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        post_shadow_tree_sha256 = sublation_run.tree_hash(registry)
        registry_index.write_bytes(baseline_bytes)
        post_rollback_tree_sha256 = sublation_run.tree_hash(registry)
        rollback_record = {
            "schema_version": "skill-sublation.routing-rollback-receipt.v1",
            "candidate_id": manifest["candidate_id"],
            "registry_root_id": "synthetic-registry",
            "registry_path": str(registry),
            "index_relative_path": "index.json",
            "pre_tree_sha256": pre_tree_sha256,
            "post_shadow_tree_sha256": post_shadow_tree_sha256,
            "post_rollback_tree_sha256": post_rollback_tree_sha256,
            "index_before_sha256": baseline_index["content_sha256"],
            "index_shadow_sha256": index["content_sha256"],
            "index_after_sha256": baseline_index["content_sha256"],
            "action": "replace_then_restore",
            "performed_at": sublation_run.utc_now(),
            "index_sha256": index["content_sha256"],
            "router_sha256": router_hash,
            "profile_set_sha256": index["profile_set_sha256"],
            "fallback_verified": True,
            "registry_rollback_verified": True,
            "legacy_catalog_unchanged": True,
        }
        rollback_path = authorization_run / "routing-rollback-receipt.json"
        sublation_run.atomic_write_json(rollback_path, rollback_record)
        rollback_receipt_sha256 = skill_router.sha256(rollback_path.read_bytes())
        rollback_event = sublation_run.append_event(
            authorization_run,
            "routing_registry_rollback_verified",
            {
                **rollback_record,
                "rollback_receipt_sha256": rollback_receipt_sha256,
            },
        )
        attestation_path = self.root / "validation" / "attestation.json"
        fixture_report_sha256 = skill_router.sha256(report_path.read_bytes())
        observation_log_sha256 = skill_router.sha256(log.read_bytes())
        attestation_report_sha256 = skill_router.sha256(attestation_path.read_bytes())
        artifact_binding = skill_router.sha256(
            {
                "candidate_id": manifest["candidate_id"],
                "candidate_tree_hash": candidate_tree_hash,
                "index_sha256": index["content_sha256"],
                "taxonomy_sha256": index["taxonomy_sha256"],
                "router_sha256": router_hash,
                "profile_set_sha256": index["profile_set_sha256"],
                "fixture_plan_sha256": fixture_plan_sha256,
                "fixture_plan_artifact_sha256": skill_router.sha256(
                    plan_path.read_bytes()
                ),
                "fixture_report_sha256": fixture_report_sha256,
                "observation_log_sha256": observation_log_sha256,
                "attestation_report_sha256": attestation_report_sha256,
                "case_id_set_sha256": report["case_id_set_sha256"],
                "authorization_receipt_sha256": receipt_record["sha256"],
                "promotion_event_hash": promotion_event["event_hash"],
                "host_adapter_id": host_adapter_id,
                "host_adapter_sha256": host_adapter_sha256,
                "fallback_receipt_sha256": fallback_receipt_sha256,
                "fallback_event_hash": fallback_event["event_hash"],
                "rollback_receipt_sha256": rollback_receipt_sha256,
                "rollback_event_hash": rollback_event["event_hash"],
                "attestation_key_id": key_id,
                "attestation_key_sha256": key_hash,
            }
        )
        review_room = self.private_root / "review-room.jsonl"
        review_events = [
            {
                "id": "review-hermes",
                "timestamp": sublation_run.utc_now(),
                "speaker": "hermes",
                "audience": "all",
                "kind": "reply",
                "text": f"[routing-review decision=approve artifact={artifact_binding}]",
            },
            {
                "id": "review-claude",
                "timestamp": sublation_run.utc_now(),
                "speaker": "claude-code",
                "audience": "all",
                "kind": "reply",
                "text": f"[routing-review decision=approve artifact={artifact_binding}]",
            },
        ]
        review_room.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in review_events
            ),
            encoding="utf-8",
        )
        shadow = {
            "status": "passed",
            "mode": "shadow",
            "sample_source": "authorized_shadow_runtime",
            "boundary": "shadow evidence only; no execution or live routing authority",
            "attestation_key_id": key_id,
            "attestation_key_sha256": key_hash,
            "index_path": "validation/index.json",
            "index_sha256": index["content_sha256"],
            "router_sha256": router_hash,
            "profile_set_sha256": index["profile_set_sha256"],
            "fixture_report_path": "validation/report.json",
            "fixture_report_sha256": fixture_report_sha256,
            "fixture_plan_path": "validation/plan.json",
            "fixture_plan_sha256": fixture_plan_sha256,
            "fixture_plan_artifact_sha256": skill_router.sha256(plan_path.read_bytes()),
            "observation_log_path": "validation/observations.jsonl",
            "observation_log_sha256": observation_log_sha256,
            "observation_event_count": 30,
            "attestation_report_path": "validation/attestation.json",
            "attestation_report_sha256": attestation_report_sha256,
            "authorization_run_dir": str(authorization_run),
            "authorization_receipt_path": str(receipt_path),
            "authorization_receipt_sha256": receipt_record["sha256"],
            "promotion_event_hash": promotion_event["event_hash"],
            "host_adapter_id": host_adapter_id,
            "host_adapter_path": str(host_adapter),
            "host_adapter_sha256": host_adapter_sha256,
            "fallback_receipt_path": str(fallback_path),
            "fallback_receipt_sha256": fallback_receipt_sha256,
            "fallback_event_hash": fallback_event["event_hash"],
            "rollback_receipt_path": str(rollback_path),
            "rollback_receipt_sha256": rollback_receipt_sha256,
            "rollback_event_hash": rollback_event["event_hash"],
            **metrics,
            "independent_reviews": [
                {
                    "reviewer": event["speaker"],
                    "message_id": event["id"],
                    "decision": "approve",
                    "room_log_path": str(review_room),
                    "message_sha256": skill_router.sha256(event),
                    "artifact_binding_sha256": artifact_binding,
                }
                for event in review_events
            ],
        }
        manifest["validation"]["routing_shadow"] = shadow
        return shadow

    def test_valid_candidate_metadata_is_self_consistent(self) -> None:
        metadata = routing_metadata()
        self.write_metadata(metadata)
        errors, warnings = routing_gate.metadata_findings(self.root, manifest_for(metadata))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_post_cutover_candidate_cannot_delete_routing_contract(self) -> None:
        manifest = {"schema_version": 3, "created_at": "2026-08-23T14:29:35Z", "validation": {"status": "draft"}}
        errors, _warnings = routing_gate.metadata_findings(self.root, manifest)
        self.assertTrue(any("routing metadata is required" in item for item in errors))

    def test_pre_cutover_candidate_is_grandfathered(self) -> None:
        manifest = {"schema_version": 3, "created_at": "2026-08-22T00:00:00Z", "validation": {"status": "draft"}}
        self.assertEqual(routing_gate.metadata_findings(self.root, manifest), ([], []))

    def test_metadata_hash_drift_is_blocked(self) -> None:
        metadata = routing_metadata()
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["routing"]["metadata_sha256"] = "sha256:" + "0" * 64
        errors, _warnings = routing_gate.metadata_findings(self.root, manifest)
        self.assertTrue(any("metadata_sha256" in item for item in errors))

    def test_approved_candidate_requires_approved_classification(self) -> None:
        metadata = routing_metadata(status="candidate")
        self.write_metadata(metadata)
        errors, _warnings = routing_gate.metadata_findings(
            self.root,
            manifest_for(metadata, validation_status="approved"),
        )
        self.assertTrue(any("require approved classification" in item for item in errors))

    def test_promotion_blocks_unapproved_metadata(self) -> None:
        metadata = routing_metadata(status="candidate")
        self.write_metadata(metadata)
        errors = routing_gate.promotion_errors(self.root, manifest_for(metadata))
        self.assertTrue(any("classification_status=approved" in item for item in errors))

    def test_first_promotion_must_remain_shadow(self) -> None:
        metadata = routing_metadata(status="approved")
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["routing"]["index_mode"] = "live"
        errors = routing_gate.promotion_errors(self.root, manifest)
        self.assertTrue(any("first routing promotion" in item for item in errors))

    def test_promotion_requires_precommitted_attestation_key(self) -> None:
        metadata = routing_metadata(status="approved")
        self.write_metadata(metadata)
        errors = routing_gate.promotion_errors(self.root, manifest_for(metadata))
        self.assertTrue(any("precommitted routing attestation key" in item for item in errors))

    def test_approved_classification_requires_two_bound_independent_reports(self) -> None:
        metadata = routing_metadata(status="approved")
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["validation"]["pre_promotion_reports"] = []
        errors = routing_gate.promotion_errors(self.root, manifest)
        self.assertTrue(any("two independent approve reports" in item for item in errors))

    def test_approved_classification_requires_unique_review_receipts(self) -> None:
        metadata = routing_metadata(status="approved")
        metadata["classification"]["evidence"] = ["review-shared"]
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["validation"]["pre_promotion_reports"] = [
            {
                "reviewer": "hermes",
                "status": "approve",
                "focus": "boundary",
                "message_id": "review-shared",
            },
            {
                "reviewer": "claude-code",
                "status": "approve",
                "focus": "implementation",
                "message_id": "review-shared",
            },
        ]
        errors = routing_gate.promotion_errors(self.root, manifest)
        self.assertTrue(any("unique bound message ids" in item for item in errors))

    def test_closure_rejects_missing_shadow_evidence(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        errors = routing_gate.closure_errors(self.root, manifest)
        self.assertTrue(any("routing_shadow is required" in item for item in errors))

    def test_closure_cannot_delete_post_cutover_routing_contract(self) -> None:
        manifest = {
            "schema_version": 3,
            "created_at": "2026-08-23T14:29:35Z",
            "validation": {"status": "observation_window"},
        }
        errors = routing_gate.closure_errors(self.root, manifest)
        self.assertTrue(any("routing metadata is required" in item for item in errors))

    def test_closure_accepts_bound_non_regressing_runtime_evidence(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        self.complete_shadow(manifest)
        self.assertEqual(
            routing_gate.closure_errors(
                self.root,
                manifest,
                attestation_key=self.attestation_key,
            ),
            [],
        )

    def test_closure_rejects_private_key_inside_candidate_package(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        self.complete_shadow(manifest)
        packaged_key = self.root / "attestation.key"
        packaged_key.write_bytes(self.attestation_key.read_bytes())
        os.chmod(packaged_key, 0o600)
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=packaged_key,
        )
        self.assertTrue(any("outside the candidate root" in item for item in errors))

    def test_fallback_signal_cannot_replace_host_execution_receipt(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        self.assertTrue(shadow["fallback_verified"])
        Path(shadow["host_adapter_path"]).unlink()
        shadow["fallback_event_hash"] = "sha256:" + "0" * 64
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("host_adapter_path is unavailable" in item for item in errors))
        self.assertTrue(
            any("host fallback execution event is absent" in item for item in errors)
        )

    def test_closure_rejects_nan_negative_latency_and_same_reviewer(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        shadow["route_accuracy"] = float("nan")
        shadow["p95_router_latency_ms"] = -1
        shadow["independent_reviews"] = [
            {"reviewer": "hermes", "message_id": "one"},
            {"reviewer": "hermes", "message_id": "two"},
        ]
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("route_accuracy must be finite" in item for item in errors))
        self.assertTrue(any("p95_router_latency_ms must be at least 0" in item for item in errors))
        self.assertTrue(any("two distinct reviewers" in item for item in errors))

    def test_closure_rejects_empty_report_and_forged_hashes(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        report = self.write_json("validation/report.json", {})
        shadow["fixture_report_sha256"] = skill_router.sha256(report.read_bytes())
        shadow["index_sha256"] = "sha256:" + "9" * 64
        shadow["router_sha256"] = "sha256:" + "8" * 64
        shadow["profile_set_sha256"] = "sha256:" + "7" * 64
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("schema_version mismatch" in item for item in errors))
        self.assertTrue(any("index_sha256 mismatch" in item for item in errors))
        self.assertTrue(any("router_sha256 mismatch" in item for item in errors))
        self.assertTrue(any("profile_set_sha256 mismatch" in item for item in errors))

    def test_closure_rejects_prompt_smuggled_into_report_result(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        report_path = self.root / "validation" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["results"][0]["prompt"] = "敏感案件原文"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shadow["fixture_report_sha256"] = skill_router.sha256(report_path.read_bytes())
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("privacy contract" in item for item in errors))

    def test_corrupt_validation_and_missing_taxonomy_become_findings(self) -> None:
        metadata = routing_metadata()
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["validation"] = []
        with mock.patch.object(routing_gate.skill_router, "load_taxonomy", side_effect=OSError("missing")):
            errors, _warnings = routing_gate.metadata_findings(self.root, manifest)
        self.assertTrue(any("taxonomy is unavailable" in item for item in errors))
        self.assertTrue(any("validation must be an object" in item for item in errors))

    def test_extreme_numeric_value_is_rejected_without_exception(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        report_path = self.root / "validation" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        huge = 10**1000
        report["metrics"]["route_accuracy"] = huge
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shadow["route_accuracy"] = huge
        shadow["fixture_report_sha256"] = skill_router.sha256(report_path.read_bytes())
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("route_accuracy must be finite" in item for item in errors))

    def test_fixture_plan_cannot_smuggle_case_text_as_runtime_identity(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        plan_path = self.root / "validation" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["cases"][0]["expected_top"] = "case:敏感案件原文"
        plan["content_sha256"] = skill_router.sha256(
            {key: value for key, value in plan.items() if key != "content_sha256"}
        )
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shadow["fixture_plan_sha256"] = plan["content_sha256"]
        shadow["fixture_plan_artifact_sha256"] = skill_router.sha256(
            plan_path.read_bytes()
        )
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("outside the bound index" in item for item in errors))

    def test_malformed_failure_codes_are_rejected_without_exception(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        report_path = self.root / "validation" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["results"][0]["failure_codes"] = [{}]
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shadow["fixture_report_sha256"] = skill_router.sha256(
            report_path.read_bytes()
        )
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("failure_codes is invalid" in item for item in errors))

    def test_false_pass_for_negative_control_is_recomputed_from_plan(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        report_path = self.root / "validation" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        negative = next(
            item for item in report["results"] if item["case_type"] == "negative_control"
        )
        negative["recommended_runtime_ids"].append("fixture:secondary-skill")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shadow["fixture_report_sha256"] = skill_router.sha256(
            report_path.read_bytes()
        )
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("failure_codes differ" in item for item in errors))
        self.assertTrue(any("passed flag differs" in item for item in errors))

    def test_shadow_manifest_rejects_extra_raw_prompt_field(self) -> None:
        metadata = routing_metadata(status="approved")
        self.write_metadata(metadata)
        manifest = manifest_for(metadata)
        manifest["validation"]["routing_shadow"] = {
            "status": "offline_passed",
            "mode": "shadow",
            "sample_source": "synthetic_fixture",
            "boundary": "offline only",
            "raw_prompt": "敏感案件原文",
        }
        errors, _warnings = routing_gate.metadata_findings(self.root, manifest)
        self.assertTrue(any("unsupported keys: raw_prompt" in item for item in errors))

    def test_self_referential_symlinks_become_findings(self) -> None:
        metadata = routing_metadata()
        manifest = manifest_for(metadata)
        os.symlink("routing-loop", self.root / "routing-loop")
        manifest["routing"]["metadata_path"] = "routing-loop"
        errors, _warnings = routing_gate.metadata_findings(self.root, manifest)
        self.assertTrue(any("must not traverse symlinks" in item for item in errors))

        key_loop = self.private_root / "key-loop"
        os.symlink("key-loop", key_loop)
        _path, key_errors = routing_gate._external_path(
            str(key_loop),
            "routing attestation key",
            kind="file",
        )
        self.assertTrue(any("must not be a symlink" in item for item in key_errors))

    def test_review_must_follow_rollback_evidence_and_use_canonical_identity(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        review = shadow["independent_reviews"][0]
        room_path = Path(review["room_log_path"])
        events = [
            json.loads(line)
            for line in room_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events[0]["speaker"] = "Hermes"
        events[0]["timestamp"] = "2020-01-01T00:00:00+00:00"
        room_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        review["reviewer"] = "Hermes"
        review["message_sha256"] = skill_router.sha256(events[0])
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("independent from builder" in item for item in errors))
        self.assertTrue(any("predates the rollback evidence" in item for item in errors))

    def test_review_timestamp_equal_to_rollback_is_not_strictly_after(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        rollback_time = next(
            event["timestamp"]
            for event in sublation_run.verified_journal_entries(
                Path(shadow["authorization_run_dir"])
            )
            if event.get("event_hash") == shadow["rollback_event_hash"]
        )
        review = shadow["independent_reviews"][0]
        room_path = Path(review["room_log_path"])
        events = [
            json.loads(line)
            for line in room_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events[0]["timestamp"] = rollback_time
        room_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        review["message_sha256"] = skill_router.sha256(events[0])
        errors = routing_gate.closure_errors(
            self.root,
            manifest,
            attestation_key=self.attestation_key,
        )
        self.assertTrue(any("predates the rollback evidence" in item for item in errors))

    def test_closure_uses_immutable_promotion_report_not_latest_report(self) -> None:
        metadata = routing_metadata(status="approved")
        manifest = manifest_for(metadata)
        shadow = self.complete_shadow(manifest)
        run_dir = Path(shadow["authorization_run_dir"])
        original_version = next(
            event["payload"]["report_version"]
            for event in sublation_run.verified_journal_entries(run_dir)
            if event.get("event_hash") == shadow["promotion_event_hash"]
        )
        run = sublation_run.load_run(run_dir)
        original = sublation_run.read_json(
            run_dir / f"report-v{original_version}.json"
        )
        newer = {
            **original,
            "report_version": original_version + 1,
            "generated_at": sublation_run.utc_now(),
            "delivery": [],
        }
        newer["report_hash"] = sublation_run.report_hash(newer)
        rendered = sublation_run.render_plain_report(newer)
        newer["plain_report_sha256"] = sublation_run.sha256(
            rendered.encode("utf-8")
        )
        sublation_run.atomic_write_json(
            run_dir / f"report-v{newer['report_version']}.json",
            newer,
        )
        sublation_run.atomic_write_text(
            run_dir / f"report-v{newer['report_version']}.md",
            rendered,
        )
        newer_summary = {
            "report_version": newer["report_version"],
            "scope_revision": newer["scope_revision"],
            "report_hash": newer["report_hash"],
            "plain_report_sha256": newer["plain_report_sha256"],
            "material_hash": newer["material_hash"],
            "state": newer["state"],
            "json_path": str(
                run_dir / f"report-v{newer['report_version']}.json"
            ),
            "markdown_path": str(
                run_dir / f"report-v{newer['report_version']}.md"
            ),
            "delivery": [],
        }
        run["reports"].append(newer_summary)
        run["latest_report"] = newer_summary
        run["revision"] += 1
        run["updated_at"] = sublation_run.utc_now()
        sublation_run.commit_run(
            run_dir,
            run,
            "report_finalized",
            {
                "report_version": newer["report_version"],
                "report_hash": newer["report_hash"],
                "scope_revision": newer["scope_revision"],
                "state": newer["state"],
                "approval_item_ids": ["A1"],
                "approval_items_hash": sublation_run.sha256(
                    newer["approval_items"]
                ),
            },
        )
        self.assertGreater(newer["report_version"], original_version)
        self.assertEqual(
            routing_gate.closure_errors(
                self.root,
                manifest,
                attestation_key=self.attestation_key,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
