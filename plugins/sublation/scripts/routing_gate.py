#!/usr/bin/env python3
"""Governance gates for routing metadata and shadow observation closure."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import skill_router
import sublation_run


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
POST_APPROVAL_STATUSES = {"approved", "promoted", "observation_window", "closed"}
ROUTING_CUTOVER = datetime(2026, 8, 23, 14, 29, 35, tzinfo=timezone.utc)
RATE_FIELDS = (
    "explicit_invocation_adherence",
    "deterministic_order_rate",
    "fallback_success_rate",
    "single_domain_top1",
    "multi_domain_top3",
    "route_accuracy",
    "baseline_route_accuracy",
    "manual_correction_rate",
    "baseline_manual_correction_rate",
)
COUNT_FIELDS = (
    "negative_control_misroutes",
    "high_risk_misroutes",
    "authority_expansion_events",
    "raw_prompt_leaks",
    "tree_drift_misses",
)
MIRRORED_REPORT_FIELDS = (
    *RATE_FIELDS,
    *COUNT_FIELDS,
    "valid_samples",
    "p95_router_latency_ms",
    "net_token_savings_status",
    "host_deferred_loading_verified",
    "registry_rollback_verified",
)
COHORT_MINIMUMS = {
    "single_domain": 10,
    "multi_domain": 3,
    "explicit": 3,
    "fallback": 3,
    "negative_control": 3,
    "high_risk": 3,
    "tree_drift": 3,
}
ROUTING_SHADOW_KEYS = {
    "status",
    "mode",
    "sample_source",
    "boundary",
    "attestation_key_id",
    "attestation_key_sha256",
    "index_path",
    "index_sha256",
    "router_sha256",
    "profile_set_sha256",
    "fixture_report_path",
    "fixture_report_sha256",
    "fixture_plan_path",
    "fixture_plan_sha256",
    "fixture_plan_artifact_sha256",
    "observation_log_path",
    "observation_log_sha256",
    "observation_event_count",
    "attestation_report_path",
    "attestation_report_sha256",
    "authorization_run_dir",
    "authorization_receipt_path",
    "authorization_receipt_sha256",
    "promotion_event_hash",
    "host_adapter_id",
    "host_adapter_path",
    "host_adapter_sha256",
    "fallback_receipt_path",
    "fallback_receipt_sha256",
    "fallback_event_hash",
    "fallback_verified",
    "rollback_receipt_path",
    "rollback_receipt_sha256",
    "rollback_event_hash",
    "independent_reviews",
    *MIRRORED_REPORT_FIELDS,
}
ATTESTATION_KEYS = {
    "schema_version",
    "status",
    "log_sha256",
    "event_count",
    "first_event_sha256",
    "last_event_sha256",
    "chain_verified",
    "hmac_verified",
    "privacy_flags_verified",
    "unique_request_ids_verified",
    "runtime_binding_true_count",
    "runtime_binding_false_count",
    "runtime_binding_null_count",
    "deterministic_replay_true_count",
    "deterministic_replay_false_count",
    "index_sha256",
    "router_sha256",
    "profile_set_sha256",
    "attestation_key_id",
    "attestation_key_sha256",
    "request_id_set_sha256",
    "result_projection_sha256",
    "plan_projection_sha256",
    "p95_latency_ms",
    "first_ts",
    "last_ts",
}
AUTHORIZATION_RECEIPT_KEYS = {
    "schema_version",
    "adapter_id",
    "channel",
    "event_id",
    "sender_id",
    "in_reply_to",
    "message",
    "received_at",
    "source_event_hash",
    "report_version",
    "report_hash",
    "report_body_hash",
    "scope_revision",
    "approval_code",
    "purpose",
    "decision",
    "candidate_id",
    "candidate_tree_hash",
    "promotion_event_hash",
    "index_sha256",
    "fixture_plan_sha256",
    "case_id_set_sha256",
    "attestation_key_id",
    "attestation_key_sha256",
    "mode",
    "attestation_mac",
}
AUTHORIZATION_REQUEST_KEYS = {
    "candidate_id",
    "candidate_tree_hash",
    "promotion_event_hash",
    "index_sha256",
    "fixture_plan_sha256",
    "case_id_set_sha256",
    "attestation_key_id",
    "attestation_key_sha256",
    "mode",
    "channel",
    "message_ref",
    "report_version",
    "report_hash",
    "report_body_hash",
    "scope_revision",
    "approval_code",
    "authorized_reply_senders",
}
ROLLBACK_RECEIPT_KEYS = {
    "schema_version",
    "candidate_id",
    "registry_root_id",
    "registry_path",
    "index_relative_path",
    "pre_tree_sha256",
    "post_shadow_tree_sha256",
    "post_rollback_tree_sha256",
    "index_before_sha256",
    "index_shadow_sha256",
    "index_after_sha256",
    "action",
    "performed_at",
    "index_sha256",
    "router_sha256",
    "profile_set_sha256",
    "fallback_verified",
    "registry_rollback_verified",
    "legacy_catalog_unchanged",
}
FALLBACK_RECEIPT_KEYS = {
    "schema_version",
    "candidate_id",
    "host_adapter_id",
    "host_adapter_sha256",
    "index_sha256",
    "case_id_set_sha256",
    "fallback_case_id_set_sha256",
    "fallback_case_count",
    "fallback_requested_count",
    "fallback_executed_count",
    "baseline_outcome_recorded_count",
    "cases",
    "performed_at",
    "execution_verified",
    "legacy_catalog_unchanged",
}
FALLBACK_CASE_KEYS = {
    "case_id",
    "router_decision",
    "baseline_selection_id",
    "outcome",
}
INDEPENDENT_REVIEW_KEYS = {
    "reviewer",
    "message_id",
    "decision",
    "room_log_path",
    "message_sha256",
    "artifact_binding_sha256",
}


def _safe_relative_file(root: Path, value: Any, label: str) -> tuple[Path | None, list[str]]:
    if not isinstance(value, str) or not value:
        return None, [f"{label} is required"]
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None, [f"{label} must be a safe relative path"]
    unresolved = root / relative
    try:
        resolved_root = root.resolve(strict=True)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None, [f"{label} must not traverse symlinks"]
        path = unresolved.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, [f"{label} is unavailable: {value}"]
    try:
        path.relative_to(resolved_root)
    except ValueError:
        return None, [f"{label} escaped candidate root"]
    if not path.is_file() or unresolved.is_symlink():
        return None, [f"{label} is unavailable: {value}"]
    return path, []


def _external_path(
    value: Any,
    label: str,
    *,
    kind: str,
) -> tuple[Path | None, list[str]]:
    if not isinstance(value, str) or not value:
        return None, [f"{label} is required"]
    try:
        unresolved = Path(value).expanduser()
        if unresolved.is_symlink():
            return None, [f"{label} must not be a symlink"]
        path = unresolved.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, [f"{label} is unavailable"]
    if kind == "file" and not path.is_file():
        return None, [f"{label} must be a regular file"]
    if kind == "directory" and not path.is_dir():
        return None, [f"{label} must be a directory"]
    return path, []


def _aware_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _created_after_cutover(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) >= ROUTING_CUTOVER


def routing_required(root: Path, manifest: dict[str, Any]) -> bool:
    """Grandfather old manifests while preventing post-cutover block deletion."""

    candidate = manifest.get("candidate")
    source_skill = manifest.get("source_skill")
    candidate_files = candidate.get("files", {}) if isinstance(candidate, dict) else {}
    source_files = source_skill.get("files", {}) if isinstance(source_skill, dict) else {}
    tracked = (
        isinstance(candidate_files, dict)
        and ("routing.json" in candidate_files or "scripts/skill_router.py" in candidate_files)
    ) or (
        isinstance(source_files, dict) and "scripts/skill_router.py" in source_files
    )
    if (root / "routing.json").exists() or tracked:
        return True
    schema_version = manifest.get("schema_version")
    if schema_version == 3:
        created_at = manifest.get("created_at")
        # Missing or malformed timestamps are never treated as proof of legacy
        # status. Only an explicit valid pre-cutover v3 timestamp is exempt.
        if not isinstance(created_at, str) or not created_at:
            return True
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) >= ROUTING_CUTOVER
    if schema_version == 2:
        return _created_after_cutover(manifest.get("created_at"))
    # Missing/unknown schema is corruption, not proof of legacy status.
    return True


def metadata_findings(root: Path, manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    routing = manifest.get("routing")
    if routing is None:
        if routing_required(root, manifest):
            return ["routing metadata is required for this post-cutover candidate"], []
        return [], []
    if not isinstance(routing, dict):
        return ["routing must be an object"], []
    errors: list[str] = []
    warnings: list[str] = []
    expected_keys = {
        "required_on_promotion",
        "metadata_path",
        "metadata_sha256",
        "classification_status",
        "index_mode",
        "tree_hash_algorithm",
        "attestation_key_id",
        "attestation_key_sha256",
    }
    unknown = sorted(set(routing) - expected_keys)
    missing = sorted(expected_keys - set(routing))
    if unknown:
        errors.append("routing has unsupported keys: " + ", ".join(unknown))
    if missing:
        errors.append("routing is missing keys: " + ", ".join(missing))
    if routing.get("required_on_promotion") is not True:
        errors.append("routing.required_on_promotion must be true when routing metadata is present")
    if routing.get("index_mode") not in {"shadow", "live"}:
        errors.append("routing.index_mode must be shadow or live")
    if routing.get("tree_hash_algorithm") != skill_router.TREE_HASH_ALGORITHM:
        errors.append("routing.tree_hash_algorithm mismatch")
    key_id = routing.get("attestation_key_id")
    key_hash = routing.get("attestation_key_sha256")
    if (key_id is None) != (key_hash is None):
        errors.append("routing attestation key id and hash must be set together")
    if key_id is not None and (
        not isinstance(key_id, str) or not skill_router.OPAQUE_ID_RE.fullmatch(key_id)
    ):
        errors.append("routing.attestation_key_id must be an opaque id or null")
    if key_hash is not None and (
        not isinstance(key_hash, str) or not SHA256_RE.fullmatch(key_hash)
    ):
        errors.append("routing.attestation_key_sha256 must be a sha256 digest or null")
    metadata_path, path_errors = _safe_relative_file(
        root, routing.get("metadata_path"), "routing.metadata_path"
    )
    errors.extend(path_errors)
    metadata: dict[str, Any] | None = None
    if metadata_path:
        try:
            metadata = skill_router.read_json(metadata_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if metadata is not None:
        # The taxonomy belongs to the routing engine, not to every routed Skill.
        try:
            taxonomy = skill_router.load_taxonomy()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"routing taxonomy is unavailable: {exc}")
            taxonomy = None
        if taxonomy is not None:
            errors.extend(skill_router.validate_metadata(metadata, taxonomy))
        actual_hash = skill_router.metadata_sha256(metadata)
        if routing.get("metadata_sha256") != actual_hash:
            errors.append("routing.metadata_sha256 does not match routing.json")
        classification = metadata.get("classification")
        metadata_status = classification.get("status") if isinstance(classification, dict) else None
        if routing.get("classification_status") != metadata_status:
            errors.append("routing.classification_status does not match routing.json")
        validation = manifest.get("validation")
        if not isinstance(validation, dict):
            errors.append("validation must be an object")
            validation = {}
        validation_status = validation.get("status")
        if validation_status in POST_APPROVAL_STATUSES and metadata_status != "approved":
            errors.append("approved/promoted routing candidates require approved classification metadata")
        if routing.get("index_mode") == "live" and metadata_status != "approved":
            errors.append("live routing index mode requires approved classification metadata")
        if metadata_status == "approved":
            evidence = (
                classification.get("evidence", [])
                if isinstance(classification, dict)
                else []
            )
            reports = validation.get("pre_promotion_reports", [])
            approved_reviews: dict[str, str] = {}
            builder = str(manifest.get("created_by") or "").strip().casefold()
            if isinstance(reports, list):
                for report in reports:
                    if not isinstance(report, dict) or report.get("status") != "approve":
                        continue
                    reviewer = report.get("reviewer")
                    message_id = report.get("message_id")
                    normalized_reviewer = (
                        reviewer.strip().casefold()
                        if isinstance(reviewer, str)
                        else ""
                    )
                    if (
                        isinstance(reviewer, str)
                        and skill_router.OPAQUE_ID_RE.fullmatch(reviewer)
                        and reviewer == normalized_reviewer
                        and normalized_reviewer != builder
                        and isinstance(message_id, str)
                        and skill_router.OPAQUE_ID_RE.fullmatch(message_id)
                        and message_id in evidence
                    ):
                        approved_reviews[normalized_reviewer] = message_id
            if len(approved_reviews) < 2 or len(set(approved_reviews.values())) < 2:
                errors.append(
                    "approved routing classification requires two independent approve reports with unique bound message ids"
                )
        if metadata_status == "suggested":
            warnings.append("routing metadata is an unreviewed deterministic suggestion")
    validation = manifest.get("validation")
    shadow = validation.get("routing_shadow") if isinstance(validation, dict) else None
    if shadow is not None:
        if not isinstance(shadow, dict):
            errors.append("validation.routing_shadow must be an object")
        else:
            unknown_shadow = sorted(set(shadow) - ROUTING_SHADOW_KEYS)
            if unknown_shadow:
                errors.append(
                    "validation.routing_shadow has unsupported keys: "
                    + ", ".join(unknown_shadow)
                )
            if shadow.get("mode") != "shadow":
                errors.append("validation.routing_shadow.mode must be shadow")
            if shadow.get("status") not in {"offline_passed", "observing", "passed"}:
                errors.append("validation.routing_shadow.status is invalid")
            if shadow.get("sample_source") not in {
                "synthetic_fixture",
                "authorized_shadow_runtime",
            }:
                errors.append("validation.routing_shadow.sample_source is invalid")
            boundary = shadow.get("boundary")
            if not isinstance(boundary, str) or not boundary:
                errors.append("validation.routing_shadow.boundary is required")
    return errors, warnings


def _number(
    shadow: dict[str, Any],
    key: str,
    errors: list[str],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> float | int | None:
    value = shadow.get(key)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = math.nan
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(numeric) or (integer and not isinstance(value, int)):
        kind = "integer" if integer else "finite numeric"
        errors.append(f"validation.routing_shadow.{key} must be {kind}")
        return None
    if minimum is not None and numeric < minimum:
        errors.append(f"validation.routing_shadow.{key} must be at least {minimum:g}")
    if maximum is not None and numeric > maximum:
        errors.append(f"validation.routing_shadow.{key} must not exceed {maximum:g}")
    return value


def _finite_numeric(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _load_report(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is invalid: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} must contain an object")
        return None
    return value


def closure_errors(
    root: Path,
    manifest: dict[str, Any],
    *,
    attestation_key: Path | None = None,
) -> list[str]:
    metadata_errors, _warnings = metadata_findings(root, manifest)
    routing = manifest.get("routing")
    if not isinstance(routing, dict) or routing.get("required_on_promotion") is not True:
        return metadata_errors
    validation = manifest.get("validation")
    if not isinstance(validation, dict):
        return ["validation is required for routing observation closure"]
    shadow = validation.get("routing_shadow")
    if not isinstance(shadow, dict):
        return ["validation.routing_shadow is required before routing observation closure"]
    errors: list[str] = list(metadata_errors)
    missing_shadow = sorted(ROUTING_SHADOW_KEYS - set(shadow))
    if missing_shadow:
        errors.append(
            "validation.routing_shadow is missing closure keys: "
            + ", ".join(missing_shadow)
        )
    if shadow.get("status") != "passed" or shadow.get("mode") != "shadow":
        errors.append("validation.routing_shadow must be status=passed and mode=shadow")
    if shadow.get("sample_source") != "authorized_shadow_runtime":
        errors.append("routing closure requires sample_source=authorized_shadow_runtime")
    if shadow.get("attestation_key_id") != routing.get("attestation_key_id"):
        errors.append("routing shadow attestation_key_id differs from the precommitted key")
    if shadow.get("attestation_key_sha256") != routing.get("attestation_key_sha256"):
        errors.append("routing shadow attestation_key_sha256 differs from the precommitted key")

    hash_fields = (
        "index_sha256",
        "router_sha256",
        "profile_set_sha256",
        "fixture_report_sha256",
        "observation_log_sha256",
        "attestation_report_sha256",
        "authorization_receipt_sha256",
        "fixture_plan_artifact_sha256",
        "fixture_plan_sha256",
        "promotion_event_hash",
        "host_adapter_sha256",
        "fallback_receipt_sha256",
        "fallback_event_hash",
        "rollback_receipt_sha256",
        "rollback_event_hash",
    )
    for key in hash_fields:
        if not isinstance(shadow.get(key), str) or not SHA256_RE.fullmatch(shadow[key]):
            errors.append(f"validation.routing_shadow.{key} must be a sha256 digest")

    index_path, path_errors = _safe_relative_file(
        root, shadow.get("index_path"), "validation.routing_shadow.index_path"
    )
    errors.extend(path_errors)
    index: dict[str, Any] | None = None
    if index_path:
        index = _load_report(index_path, "routing index", errors)
        if index is not None:
            try:
                taxonomy = skill_router.load_taxonomy()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"routing taxonomy is unavailable: {exc}")
            else:
                index_errors = skill_router.validate_index(index, taxonomy)
                errors.extend(f"routing index: {item}" for item in index_errors)
            if index.get("mode") != "shadow":
                errors.append("routing closure index must be shadow")
            if not index.get("entries"):
                errors.append("routing closure index must contain at least one bound Skill profile")
            if shadow.get("index_sha256") != index.get("content_sha256"):
                errors.append("validation.routing_shadow.index_sha256 mismatch")
            if shadow.get("router_sha256") != index.get("router_sha256"):
                errors.append("validation.routing_shadow.router_sha256 mismatch with index")
            if shadow.get("profile_set_sha256") != index.get("profile_set_sha256"):
                errors.append("validation.routing_shadow.profile_set_sha256 mismatch with index")

    # The engine is owned by the active skill-sublation installation. Ordinary
    # Skill candidates must not carry private copies of the central router.
    router_path = Path(skill_router.__file__).resolve()
    if shadow.get("router_sha256") != skill_router.sha256(router_path.read_bytes()):
        errors.append("validation.routing_shadow.router_sha256 mismatch with active routing engine")

    plan_path, path_errors = _safe_relative_file(
        root,
        shadow.get("fixture_plan_path"),
        "validation.routing_shadow.fixture_plan_path",
    )
    errors.extend(path_errors)
    fixture_plan: dict[str, Any] | None = None
    if plan_path:
        if shadow.get("fixture_plan_artifact_sha256") != skill_router.sha256(
            plan_path.read_bytes()
        ):
            errors.append("validation.routing_shadow.fixture_plan_artifact_sha256 mismatch")
        fixture_plan = _load_report(plan_path, "routing fixture plan", errors)
        if fixture_plan is not None:
            known_runtime_ids = {
                entry.get("runtime_id")
                for entry in (index or {}).get("entries", [])
                if isinstance(entry, dict) and isinstance(entry.get("runtime_id"), str)
            }
            plan_contract_errors = skill_router.validate_fixture_plan(
                fixture_plan,
                known_runtime_ids=known_runtime_ids,
            )
            errors.extend(plan_contract_errors)
            if plan_contract_errors:
                fixture_plan = None
            else:
                if fixture_plan.get("content_sha256") != shadow.get("fixture_plan_sha256"):
                    errors.append("routing fixture plan content hash mismatch")
                if fixture_plan.get("sample_source") != "authorized_shadow_runtime":
                    errors.append("routing closure requires an authorized runtime fixture plan")
                if fixture_plan.get("index_sha256") != shadow.get("index_sha256"):
                    errors.append("routing fixture plan index_sha256 mismatch")

    report_path, path_errors = _safe_relative_file(
        root, shadow.get("fixture_report_path"), "validation.routing_shadow.fixture_report_path"
    )
    errors.extend(path_errors)
    report: dict[str, Any] | None = None
    derived_metrics: dict[str, Any] = {}
    if report_path:
        if shadow.get("fixture_report_sha256") != skill_router.sha256(report_path.read_bytes()):
            errors.append("validation.routing_shadow.fixture_report_sha256 mismatch")
        report = _load_report(report_path, "routing fixture report", errors)
        if report is not None:
            report_contract_errors = skill_router.validate_fixture_report(report)
            errors.extend(report_contract_errors)
            if report_contract_errors:
                report = None
            elif report.get("sample_source") != "authorized_shadow_runtime":
                errors.append("routing fixture report is not authorized shadow runtime evidence")
        if report is not None:
            authorization_evidence = report.get("authorization_evidence")
            if not isinstance(authorization_evidence, list) or not authorization_evidence or not all(
                isinstance(item, str) and item for item in authorization_evidence
            ):
                errors.append("routing fixture report authorization_evidence is required")
            if report.get("index_sha256") != shadow.get("index_sha256"):
                errors.append("routing fixture report index_sha256 mismatch")
            if report.get("fixture_plan_sha256") != shadow.get("fixture_plan_sha256"):
                errors.append("routing fixture report fixture_plan_sha256 mismatch")
            if report.get("failed") != 0 or report.get("raw_prompt_leaks") != 0:
                errors.append("routing fixture report must have zero failures and raw prompt leaks")
            total = report.get("total")
            passed = report.get("passed")
            failed = report.get("failed")
            if all(isinstance(item, int) and not isinstance(item, bool) for item in (total, passed, failed)):
                if passed + failed != total:
                    errors.append("routing fixture report passed/failed summary mismatch")
                expected_rate = passed / total if total else 0.0
                if report.get("pass_rate") != expected_rate:
                    errors.append("routing fixture report pass_rate summary mismatch")
            results = report.get("results")
            if not isinstance(results, list) or not results or len(results) != report.get("total"):
                errors.append("routing fixture report results must match total")
            elif all(isinstance(item, dict) for item in results):
                case_ids = [item.get("case_id") for item in results]
                if not all(isinstance(item, str) for item in case_ids) or len(set(case_ids)) != len(case_ids):
                    errors.append("routing fixture report case ids must be present and unique")
                elif report.get("case_id_set_sha256") != skill_router.sha256(sorted(case_ids)):
                    errors.append("routing fixture report case_id_set_sha256 mismatch")
                projection_keys = (
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
                projections = [
                    {key: item.get(key) for key in projection_keys}
                    for item in results
                ]
                if report.get("result_projection_sha256") != skill_router.sha256(
                    sorted(projections, key=lambda item: str(item["case_id"]))
                ):
                    errors.append("routing fixture report result_projection_sha256 mismatch")
                if any(item.get("passed") is not True for item in results if isinstance(item, dict)):
                    errors.append("routing fixture report contains a failed result")
                if report.get("passed") != sum(
                    1 for item in results if isinstance(item, dict) and item.get("passed") is True
                ):
                    errors.append("routing fixture report passed summary mismatch")
                plan_cases = (
                    fixture_plan.get("cases", [])
                    if isinstance(fixture_plan, dict)
                    else []
                )
                plan_by_id = {
                    item.get("case_id"): item
                    for item in plan_cases
                    if isinstance(item, dict) and isinstance(item.get("case_id"), str)
                }
                if set(plan_by_id) != set(case_ids):
                    errors.append("routing fixture report case ids differ from the authorized plan")
                cohort_counts: dict[str, int] = {}
                known_runtime_ids = {
                    entry.get("runtime_id")
                    for entry in (index or {}).get("entries", [])
                    if isinstance(entry, dict) and isinstance(entry.get("runtime_id"), str)
                }
                for plan_case in plan_by_id.values():
                    planned_ids = [
                        plan_case.get("expected_top"),
                        *plan_case.get("expected_contains", []),
                        *plan_case.get("expected_excludes", []),
                    ]
                    if any(
                        planned is not None and planned not in known_runtime_ids
                        for planned in planned_ids
                    ):
                        errors.append("routing fixture plan references an identity outside the bound index")
                for item in results:
                    case_type = item.get("case_type")
                    cohort_counts[str(case_type)] = cohort_counts.get(str(case_type), 0) + 1
                    recommended = item.get("recommended_runtime_ids", [])
                    if isinstance(recommended, list) and all(
                        isinstance(runtime_id, str) for runtime_id in recommended
                    ) and any(
                        runtime_id not in known_runtime_ids for runtime_id in recommended
                    ):
                        errors.append("routing fixture report recommends an identity outside the bound index")
                    plan_case = plan_by_id.get(item.get("case_id"))
                    expected_failures: list[str] = []
                    if isinstance(plan_case, dict):
                        if plan_case.get("case_type") != case_type:
                            errors.append("routing fixture result cohort differs from the authorized plan")
                        expected_decision = plan_case.get("expected_decision")
                        if expected_decision is not None and item.get("decision") != expected_decision:
                            expected_failures.append("decision_mismatch")
                        expected_top = plan_case.get("expected_top")
                        if expected_top is not None and (
                            not isinstance(recommended, list)
                            or not recommended
                            or recommended[0] != expected_top
                        ):
                            expected_failures.append("top_mismatch")
                        for expected in plan_case.get("expected_contains", []):
                            if not isinstance(recommended, list) or expected not in recommended:
                                expected_failures.append("missing_expected")
                        for forbidden in plan_case.get("expected_excludes", []):
                            if isinstance(recommended, list) and forbidden in recommended:
                                expected_failures.append("forbidden_present")
                        expected_safety = plan_case.get("expected_safety_gate")
                        if expected_safety is not None and item.get("safety_gate_required") is not expected_safety:
                            expected_failures.append("safety_gate_mismatch")
                    expected_failure_codes = sorted(set(expected_failures))
                    if item.get("failure_codes") != expected_failure_codes:
                        errors.append("routing fixture failure_codes differ from plan-derived failures")
                    if item.get("passed") is not (not expected_failure_codes):
                        errors.append("routing fixture passed flag differs from plan-derived outcome")
                    if item.get("deterministic_replay_verified") is not True:
                        errors.append("routing fixture result is not deterministic on replay")
                    if case_type in {"fallback", "tree_drift"} and item.get("decision") != "fallback":
                        errors.append(f"routing fixture {case_type} cohort must fall back")
                    if case_type == "tree_drift":
                        if item.get("runtime_binding_verified") is not False:
                            errors.append("routing fixture tree_drift cohort must detect a binding failure")
                    elif item.get("runtime_binding_verified") is not True:
                        errors.append("non-drift routing fixture cases require verified runtime bindings")
                    if case_type == "high_risk" and item.get("safety_gate_required") is not True:
                        errors.append("routing fixture high_risk cohort must require the safety gate")
                    if case_type == "multi_domain" and (
                        item.get("decision") not in {"compose_or_clarify", "clarify"}
                        or not isinstance(recommended, list)
                        or len(recommended) < 2
                    ):
                        errors.append("routing fixture multi_domain cohort must retain at least two candidates")
                for cohort, minimum in COHORT_MINIMUMS.items():
                    if cohort_counts.get(cohort, 0) < minimum:
                        errors.append(
                            f"routing fixture cohort {cohort} requires at least {minimum} cases"
                        )
                def _cohort_rate(name: str, predicate: Any) -> float:
                    selected = [item for item in results if item.get("case_type") == name]
                    if not selected:
                        return 1.0
                    return sum(bool(predicate(item)) for item in selected) / len(selected)

                derived_metrics = {
                    "explicit_invocation_adherence": _cohort_rate(
                        "explicit", lambda item: item.get("passed") is True
                    ),
                    "deterministic_order_rate": sum(
                        item.get("deterministic_replay_verified") is True
                        for item in results
                    )
                    / len(results),
                    "fallback_success_rate": _cohort_rate(
                        "fallback",
                        lambda item: item.get("passed") is True
                        and item.get("decision") == "fallback",
                    ),
                    "negative_control_misroutes": sum(
                        item.get("case_type") == "negative_control"
                        and item.get("passed") is not True
                        for item in results
                    ),
                    "high_risk_misroutes": sum(
                        item.get("case_type") == "high_risk"
                        and item.get("passed") is not True
                        for item in results
                    ),
                    "authority_expansion_events": 0,
                    "raw_prompt_leaks": 0,
                    "tree_drift_misses": sum(
                        item.get("case_type") == "tree_drift"
                        and item.get("passed") is not True
                        for item in results
                    ),
                    "valid_samples": len(results),
                    "single_domain_top1": _cohort_rate(
                        "single_domain", lambda item: item.get("passed") is True
                    ),
                    "multi_domain_top3": _cohort_rate(
                        "multi_domain", lambda item: item.get("passed") is True
                    ),
                    "route_accuracy": sum(
                        item.get("passed") is True for item in results
                    )
                    / len(results),
                    "manual_correction_rate": 1.0
                    - sum(item.get("passed") is True for item in results)
                    / len(results),
                }
            metrics = report.get("metrics")
            if not isinstance(metrics, dict):
                errors.append("routing fixture report metrics are required")
            else:
                for key in MIRRORED_REPORT_FIELDS:
                    if metrics.get(key) != shadow.get(key):
                        errors.append(f"routing fixture report metric mismatch: {key}")
                if metrics.get("raw_prompt_leaks") != report.get("raw_prompt_leaks"):
                    errors.append("routing fixture report raw_prompt_leaks summary mismatch")
                if metrics.get("route_accuracy") != report.get("pass_rate"):
                    errors.append("routing fixture report route_accuracy summary mismatch")
                for key, expected in derived_metrics.items():
                    if metrics.get(key) != expected:
                        errors.append(f"routing fixture report derived metric mismatch: {key}")

    observation_path, path_errors = _safe_relative_file(
        root, shadow.get("observation_log_path"), "validation.routing_shadow.observation_log_path"
    )
    errors.extend(path_errors)
    if observation_path:
        if shadow.get("observation_log_sha256") != skill_router.sha256(observation_path.read_bytes()):
            errors.append("validation.routing_shadow.observation_log_sha256 mismatch")
        if observation_path.stat().st_mode & 0o077:
            errors.append("routing observation log must not be group/world accessible")

    attestation_path, path_errors = _safe_relative_file(
        root, shadow.get("attestation_report_path"), "validation.routing_shadow.attestation_report_path"
    )
    errors.extend(path_errors)
    attestation: dict[str, Any] | None = None
    if attestation_path:
        if shadow.get("attestation_report_sha256") != skill_router.sha256(attestation_path.read_bytes()):
            errors.append("validation.routing_shadow.attestation_report_sha256 mismatch")
        attestation = _load_report(attestation_path, "routing attestation report", errors)
        if attestation is not None:
            if set(attestation) != ATTESTATION_KEYS:
                errors.append("routing attestation report keys do not match the privacy contract")
            if attestation.get("schema_version") != skill_router.OBSERVATION_VERIFICATION_SCHEMA_VERSION:
                errors.append("routing attestation report schema_version mismatch")
            expected_values = {
                "status": "passed",
                "chain_verified": True,
                "hmac_verified": True,
                "privacy_flags_verified": True,
                "unique_request_ids_verified": True,
            }
            for key, expected in expected_values.items():
                if attestation.get(key) != expected:
                    errors.append(f"routing attestation report {key} mismatch")
            if attestation.get("log_sha256") != shadow.get("observation_log_sha256"):
                errors.append("routing attestation report log_sha256 mismatch")
            for key in ("index_sha256", "router_sha256", "profile_set_sha256"):
                if attestation.get(key) != shadow.get(key):
                    errors.append(f"routing attestation report {key} mismatch")
            for key in ("attestation_key_id", "attestation_key_sha256"):
                if attestation.get(key) != shadow.get(key):
                    errors.append(f"routing attestation report {key} mismatch")
            if report is not None and attestation.get("request_id_set_sha256") != report.get("case_id_set_sha256"):
                errors.append("routing observation request ids do not match fixture case ids")
            if report is not None and attestation.get("result_projection_sha256") != report.get("result_projection_sha256"):
                errors.append("routing observation results do not match the fixture report")
            if isinstance(fixture_plan, dict):
                plan_projection = [
                    {
                        "case_id": item["case_id"],
                        "case_type": item["case_type"],
                        "prompt_fingerprint": item["prompt_fingerprint"],
                    }
                    for item in fixture_plan.get("cases", [])
                ]
                if attestation.get("plan_projection_sha256") != skill_router.sha256(
                    sorted(plan_projection, key=lambda item: item["case_id"])
                ):
                    errors.append("routing observation prompt fingerprints differ from the authorized plan")
            if attestation.get("p95_latency_ms") != shadow.get("p95_router_latency_ms"):
                errors.append("routing attestation p95 latency differs from the shadow metrics")
            if report is not None and isinstance(report.get("metrics"), dict) and (
                attestation.get("p95_latency_ms")
                != report["metrics"].get("p95_router_latency_ms")
            ):
                errors.append("routing attestation p95 latency differs from the fixture report")
            if report is not None and isinstance(report.get("results"), list):
                expected_true = sum(
                    isinstance(item, dict) and item.get("runtime_binding_verified") is True
                    for item in report["results"]
                )
                expected_false = sum(
                    isinstance(item, dict) and item.get("runtime_binding_verified") is False
                    for item in report["results"]
                )
                expected_null = sum(
                    isinstance(item, dict) and item.get("runtime_binding_verified") is None
                    for item in report["results"]
                )
                if attestation.get("runtime_binding_true_count") != expected_true:
                    errors.append("routing attestation true binding count mismatch")
                if attestation.get("runtime_binding_false_count") != expected_false:
                    errors.append("routing attestation false binding count mismatch")
                if attestation.get("runtime_binding_null_count") != expected_null:
                    errors.append("routing attestation null binding count mismatch")
                expected_deterministic = sum(
                    isinstance(item, dict)
                    and item.get("deterministic_replay_verified") is True
                    for item in report["results"]
                )
                if attestation.get("deterministic_replay_true_count") != expected_deterministic:
                    errors.append("routing attestation deterministic replay count mismatch")
                if attestation.get("deterministic_replay_false_count") != len(report["results"]) - expected_deterministic:
                    errors.append("routing attestation nondeterministic replay count mismatch")
            promoted_at = validation.get("promoted_at")
            try:
                promoted_time = _aware_utc(promoted_at)
                first_time = _aware_utc(attestation.get("first_ts"))
                last_time = _aware_utc(attestation.get("last_ts"))
            except (TypeError, ValueError):
                errors.append("routing observation timestamps or promoted_at are invalid")
            else:
                if first_time < promoted_time:
                    errors.append("routing observation events predate promotion")
                if last_time > datetime.now(timezone.utc) + timedelta(minutes=5):
                    errors.append("routing observation events contain a future timestamp")
    if attestation_key is None:
        errors.append("routing attestation key is required for closure verification")
    elif observation_path:
        resolved_key, key_errors = _external_path(
            str(attestation_key), "routing attestation key", kind="file"
        )
        errors.extend(key_errors)
        if resolved_key is not None:
            try:
                resolved_key.relative_to(root.resolve())
            except ValueError:
                pass
            else:
                errors.append("routing attestation key must be stored outside the candidate root")
            try:
                verified = skill_router.verify_observation_log(
                    observation_path,
                    resolved_key,
                    key_id=str(shadow.get("attestation_key_id") or "invalid"),
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"routing observation live verification failed: {exc}")
            else:
                if attestation is None or verified != attestation:
                    errors.append("routing attestation report does not match live HMAC verification")

    authorization_run, path_errors = _external_path(
        shadow.get("authorization_run_dir"),
        "validation.routing_shadow.authorization_run_dir",
        kind="directory",
    )
    errors.extend(path_errors)
    authorization_receipt, path_errors = _external_path(
        shadow.get("authorization_receipt_path"),
        "validation.routing_shadow.authorization_receipt_path",
        kind="file",
    )
    errors.extend(path_errors)
    receipt: dict[str, Any] | None = None
    receipt_record: dict[str, str] | None = None
    journal: list[dict[str, Any]] | None = None
    promotion_event: dict[str, Any] | None = None
    fallback_event: dict[str, Any] | None = None
    rollback_event: dict[str, Any] | None = None
    started_event: dict[str, Any] | None = None
    authorization_request: dict[str, Any] | None = None
    approval_event: dict[str, Any] | None = None
    if authorization_run is not None:
        try:
            authorization_run.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            errors.append("routing authorization run must be stored outside the candidate root")
        try:
            journal = sublation_run.verified_journal_entries(authorization_run)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"routing authorization journal verification failed: {exc}")
    if authorization_run is not None and authorization_receipt is not None:
        try:
            receipt, receipt_record = sublation_run.verify_receipt_attestation(
                authorization_run,
                authorization_receipt,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"routing authorization receipt verification failed: {exc}")
        else:
            if set(receipt) != AUTHORIZATION_RECEIPT_KEYS:
                errors.append("routing authorization receipt keys do not match the privacy contract")
            if receipt_record.get("sha256") != shadow.get("authorization_receipt_sha256"):
                errors.append("routing authorization receipt sha256 mismatch")

    if journal is not None:
        promotion_event = next(
            (
                event
                for event in journal
                if event.get("event_hash") == shadow.get("promotion_event_hash")
                and event.get("type") in {"promotion_succeeded", "promotion_recovered"}
            ),
            None,
        )
        if promotion_event is None:
            errors.append("routing promotion event is absent from the signed run journal")
        else:
            promotion_payload = promotion_event.get("payload")
            expected_promotion = {
                "candidate_id": manifest.get("candidate_id"),
                "routing_metadata_sha256": routing.get("metadata_sha256"),
                "routing_attestation_key_id": shadow.get("attestation_key_id"),
                "routing_attestation_key_sha256": shadow.get("attestation_key_sha256"),
                "routing_index_mode": "shadow",
            }
            if not isinstance(promotion_payload, dict):
                errors.append("routing promotion event payload is invalid")
            else:
                for key, expected in expected_promotion.items():
                    if promotion_payload.get(key) != expected:
                        errors.append(f"routing promotion event binding mismatch: {key}")
                if not isinstance(promotion_payload.get("candidate_tree_hash"), str) or not SHA256_RE.fullmatch(
                    promotion_payload["candidate_tree_hash"]
                ):
                    errors.append("routing promotion event candidate_tree_hash is invalid")
                if not isinstance(promotion_payload.get("post_tree_hash"), str) or not SHA256_RE.fullmatch(
                    promotion_payload["post_tree_hash"]
                ):
                    errors.append("routing promotion event post_tree_hash is invalid")
                required_promotion_fields = {
                    "item_id",
                    "rollback_path",
                    "report_version",
                    "report_hash",
                    "report_body_hash",
                    "scope_revision",
                    "approval_snapshot_hash",
                    "run_revision",
                    "run_snapshot",
                }
                missing_promotion_fields = sorted(
                    required_promotion_fields - set(promotion_payload)
                )
                if missing_promotion_fields:
                    errors.append(
                        "routing promotion event lacks execution-pipeline fields: "
                        + ", ".join(missing_promotion_fields)
                    )
                run_snapshot = promotion_payload.get("run_snapshot")
                if not isinstance(run_snapshot, dict):
                    errors.append("routing promotion event run_snapshot is invalid")
                else:
                    if run_snapshot.get("revision") != promotion_payload.get("run_revision"):
                        errors.append("routing promotion event run revision mismatch")
                    if run_snapshot.get("state") not in {
                        "OBSERVING",
                        "USER_DECISION_REQUIRED",
                    }:
                        errors.append(
                            "routing promotion event did not enter an allowed post-execution state"
                        )
                    snapshot_approval = run_snapshot.get("approval")
                    if not isinstance(snapshot_approval, dict) or promotion_payload.get(
                        "item_id"
                    ) not in snapshot_approval.get("authorized_scope", []):
                        errors.append("routing promotion event lacks receipt-derived authority")

                try:
                    durable_run = sublation_run.load_run(authorization_run)
                    report_version = promotion_payload.get("report_version")
                    if (
                        not isinstance(report_version, int)
                        or isinstance(report_version, bool)
                        or report_version < 1
                    ):
                        raise ValueError("promotion report_version is invalid")
                    promotion_report_path = authorization_run / (
                        f"report-v{report_version}.json"
                    )
                    promotion_report = sublation_run.read_json(promotion_report_path)
                    sublation_run.verify_plain_report(
                        authorization_run, promotion_report
                    )
                    approval = sublation_run.read_json(
                        authorization_run / "approval.json"
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"routing promotion durable state is invalid: {exc}")
                else:
                    expected_report_binding = {
                        "report_version": promotion_report.get("report_version"),
                        "report_hash": promotion_report.get("report_hash"),
                        "report_body_hash": promotion_report.get("plain_report_sha256"),
                        "scope_revision": promotion_report.get("scope_revision"),
                    }
                    for key, expected in expected_report_binding.items():
                        if promotion_payload.get(key) != expected:
                            errors.append(f"routing promotion report binding mismatch: {key}")
                    report_items = promotion_report.get("approval_items")
                    report_item = next(
                        (
                            item
                            for item in report_items
                            if isinstance(item, dict)
                            and item.get("item_id") == promotion_payload.get("item_id")
                        ),
                        None,
                    ) if isinstance(report_items, list) else None
                    if not isinstance(report_item, dict):
                        errors.append("routing promotion item is absent from the approved report")
                    else:
                        expected_item = {
                            "candidate_id": manifest.get("candidate_id"),
                            "candidate_tree_hash": promotion_payload.get("candidate_tree_hash"),
                            "approval_snapshot_hash": promotion_payload.get("approval_snapshot_hash"),
                        }
                        for key, expected in expected_item.items():
                            if report_item.get(key) != expected:
                                errors.append(f"routing promotion item binding mismatch: {key}")
                        snapshot_payload = dict(report_item)
                        supplied_snapshot_hash = snapshot_payload.pop(
                            "approval_snapshot_hash", None
                        )
                        if supplied_snapshot_hash != sublation_run.sha256(snapshot_payload):
                            errors.append("routing promotion approval snapshot hash is invalid")
                        target_path, target_errors = _external_path(
                            report_item.get("target_path"),
                            "routing promoted target path",
                            kind="directory",
                        )
                        errors.extend(target_errors)
                        if target_path is not None and sublation_run.tree_hash(
                            target_path
                        ) != promotion_payload.get("post_tree_hash"):
                            errors.append("routing promoted target drifted after promotion")
                        rollback_path, rollback_errors = _external_path(
                            promotion_payload.get("rollback_path"),
                            "routing promotion rollback path",
                            kind="directory",
                        )
                        errors.extend(rollback_errors)
                        if rollback_path is not None and sublation_run.tree_hash(
                            rollback_path
                        ) != report_item.get("target_baseline_hash"):
                            errors.append("routing promotion rollback snapshot drifted")
                    decisions = approval.get("decisions")
                    decision = (
                        decisions.get(str(promotion_payload.get("item_id")))
                        if isinstance(decisions, dict)
                        else None
                    )
                    if (
                        not isinstance(decision, dict)
                        or decision.get("decision") != "approve"
                        or decision.get("execution") != "succeeded"
                        or decision.get("post_tree_hash")
                        != promotion_payload.get("post_tree_hash")
                    ):
                        errors.append("routing promotion approval state is not a succeeded receipt-derived decision")
                    started_event = next(
                        (
                            event
                            for event in journal
                            if event.get("type") == "promotion_started"
                            and isinstance(event.get("payload"), dict)
                            and event["payload"].get("item_id")
                            == promotion_payload.get("item_id")
                            and event["payload"].get("report_hash")
                            == promotion_payload.get("report_hash")
                        ),
                        None,
                    )
                    if started_event is None:
                        errors.append("routing promotion lacks a bound promotion_started event")
                    else:
                        if not (
                            isinstance(started_event.get("sequence"), int)
                            and isinstance(promotion_event.get("sequence"), int)
                            and started_event["sequence"] < promotion_event["sequence"]
                        ):
                            errors.append(
                                "routing promotion_started does not precede promotion success"
                            )
                        authorization_ids = started_event.get("payload", {}).get(
                            "authorization_event_ids"
                        )
                        if not isinstance(authorization_ids, list) or not authorization_ids:
                            errors.append("routing promotion lacks user authorization event ids")
                        else:
                            durable_approval_ids = {
                                event.get("payload", {}).get("event_id")
                                for event in journal
                                if event.get("type") == "approval_receipt_recorded"
                                and isinstance(event.get("payload"), dict)
                                and event["payload"].get("report_hash")
                                == promotion_payload.get("report_hash")
                            }
                            if not set(authorization_ids) <= durable_approval_ids:
                                errors.append("routing promotion authorization ids are absent from the durable journal")

        authorization_request = next(
            (
                event
                for event in journal
                if event.get("type") == "routing_shadow_authorization_requested"
                and isinstance(event.get("payload"), dict)
                and receipt is not None
                and event["payload"].get("message_ref") == receipt.get("in_reply_to")
            ),
            None,
        )
        if authorization_request is None:
            errors.append("routing shadow authorization request is absent from the signed run journal")
        elif receipt is not None:
            request_payload = authorization_request["payload"]
            if not (
                isinstance(authorization_request.get("sequence"), int)
                and isinstance((promotion_event or {}).get("sequence"), int)
                and authorization_request["sequence"]
                > promotion_event["sequence"]
            ):
                errors.append("routing shadow authorization request does not follow promotion")
            if set(request_payload) != AUTHORIZATION_REQUEST_KEYS:
                errors.append("routing shadow authorization request keys are invalid")
            expected_request = {
                "candidate_id": manifest.get("candidate_id"),
                "candidate_tree_hash": (
                    promotion_event.get("payload", {}).get("candidate_tree_hash")
                    if isinstance(promotion_event, dict)
                    else None
                ),
                "promotion_event_hash": shadow.get("promotion_event_hash"),
                "index_sha256": shadow.get("index_sha256"),
                "fixture_plan_sha256": shadow.get("fixture_plan_sha256"),
                "case_id_set_sha256": report.get("case_id_set_sha256") if report else None,
                "attestation_key_id": shadow.get("attestation_key_id"),
                "attestation_key_sha256": shadow.get("attestation_key_sha256"),
                "mode": "shadow",
            }
            for key, expected in expected_request.items():
                if request_payload.get(key) != expected:
                    errors.append(f"routing authorization request binding mismatch: {key}")
            if receipt.get("channel") != request_payload.get("channel"):
                errors.append("routing authorization receipt channel differs from request")
            if receipt.get("report_version") != request_payload.get("report_version"):
                errors.append("routing authorization receipt report_version differs from request")
            for key in ("report_hash", "report_body_hash", "scope_revision", "approval_code"):
                if receipt.get(key) != request_payload.get(key):
                    errors.append(f"routing authorization receipt {key} differs from request")
            allowed_senders = request_payload.get("authorized_reply_senders")
            if not isinstance(allowed_senders, list) or receipt.get("sender_id") not in allowed_senders:
                errors.append("routing authorization receipt sender is not authorized")

        if receipt is not None:
            expected_receipt = {
                "purpose": "routing_shadow_observation",
                "decision": "approve",
                "candidate_id": manifest.get("candidate_id"),
                "candidate_tree_hash": (
                    promotion_event.get("payload", {}).get("candidate_tree_hash")
                    if isinstance(promotion_event, dict)
                    else None
                ),
                "promotion_event_hash": shadow.get("promotion_event_hash"),
                "index_sha256": shadow.get("index_sha256"),
                "fixture_plan_sha256": shadow.get("fixture_plan_sha256"),
                "case_id_set_sha256": report.get("case_id_set_sha256") if report else None,
                "attestation_key_id": shadow.get("attestation_key_id"),
                "attestation_key_sha256": shadow.get("attestation_key_sha256"),
                "mode": "shadow",
            }
            for key, expected in expected_receipt.items():
                if receipt.get(key) != expected:
                    errors.append(f"routing authorization receipt binding mismatch: {key}")
            evidence = report.get("authorization_evidence") if report else None
            if not isinstance(evidence, list) or receipt.get("event_id") not in evidence:
                errors.append("routing report is not bound to the signed authorization event")
            approval_event = next(
                (
                    event
                    for event in journal
                    if event.get("type") == "approval_receipt_recorded"
                    and isinstance(event.get("payload"), dict)
                    and event["payload"].get("event_id") == receipt.get("event_id")
                ),
                None,
            )
            if approval_event is None:
                errors.append("routing authorization receipt is absent from the durable journal")
            else:
                if not (
                    isinstance(approval_event.get("sequence"), int)
                    and isinstance((authorization_request or {}).get("sequence"), int)
                    and approval_event["sequence"]
                    > authorization_request["sequence"]
                ):
                    errors.append(
                        "routing authorization receipt record does not follow its request"
                    )
                approval_payload = approval_event["payload"]
                expected_approval = {
                    "event_signature": receipt.get("attestation_mac"),
                    "channel": receipt.get("channel"),
                    "sender_id": receipt.get("sender_id"),
                    "in_reply_to": receipt.get("in_reply_to"),
                    "report_version": receipt.get("report_version"),
                    "report_hash": receipt.get("report_hash"),
                    "report_body_hash": receipt.get("report_body_hash"),
                    "scope_revision": receipt.get("scope_revision"),
                    "receipt_evidence": receipt_record,
                    "purpose": "routing_shadow_observation",
                    "decision": "approve",
                }
                for key, expected in expected_approval.items():
                    if approval_payload.get(key) != expected:
                        errors.append(f"routing authorization journal binding mismatch: {key}")
            try:
                receipt_time = _aware_utc(receipt.get("received_at"))
                promotion_time = _aware_utc(
                    (promotion_event or {}).get("timestamp")
                )
                request_time = _aware_utc(
                    (authorization_request or {}).get("timestamp")
                )
                approval_time = _aware_utc(
                    (approval_event or {}).get("timestamp")
                )
                first_event_time = _aware_utc((attestation or {}).get("first_ts"))
            except (TypeError, ValueError):
                errors.append("routing authorization receipt timestamp is invalid")
            else:
                if request_time < promotion_time:
                    errors.append("routing authorization request predates the bound promotion")
                if receipt_time < request_time:
                    errors.append("routing authorization receipt predates its request")
                if approval_time < receipt_time:
                    errors.append("routing authorization journal record predates the receipt")
                if receipt_time < promotion_time:
                    errors.append("routing authorization receipt predates the bound promotion")
                if receipt_time > first_event_time:
                    errors.append("routing authorization receipt postdates observed events")

        host_adapter_id = shadow.get("host_adapter_id")
        if (
            not isinstance(host_adapter_id, str)
            or not skill_router.OPAQUE_ID_RE.fullmatch(host_adapter_id)
            or host_adapter_id != host_adapter_id.strip().casefold()
        ):
            errors.append("routing host_adapter_id must be a canonical opaque id")
        host_adapter_path, adapter_path_errors = _external_path(
            shadow.get("host_adapter_path"),
            "validation.routing_shadow.host_adapter_path",
            kind="file",
        )
        errors.extend(adapter_path_errors)
        if host_adapter_path is not None:
            try:
                host_adapter_path.relative_to(root.resolve())
            except ValueError:
                pass
            else:
                errors.append("routing host adapter must be outside the candidate root")
            if skill_router.sha256(host_adapter_path.read_bytes()) != shadow.get(
                "host_adapter_sha256"
            ):
                errors.append("routing host adapter sha256 mismatch")

        fallback_receipt, fallback_path_errors = _external_path(
            shadow.get("fallback_receipt_path"),
            "validation.routing_shadow.fallback_receipt_path",
            kind="file",
        )
        errors.extend(fallback_path_errors)
        fallback_record: dict[str, Any] | None = None
        if fallback_receipt is not None:
            if authorization_run is None or not sublation_run.path_is_inside(
                fallback_receipt, authorization_run
            ):
                errors.append(
                    "routing fallback receipt must be inside the signed authorization run"
                )
            if skill_router.sha256(fallback_receipt.read_bytes()) != shadow.get(
                "fallback_receipt_sha256"
            ):
                errors.append("routing fallback receipt sha256 mismatch")
            fallback_record = _load_report(
                fallback_receipt, "routing fallback execution receipt", errors
            )
            if (
                fallback_record is not None
                and set(fallback_record) != FALLBACK_RECEIPT_KEYS
            ):
                errors.append("routing fallback execution receipt keys are invalid")
                fallback_record = None

        fallback_event = next(
            (
                event
                for event in journal
                if event.get("event_hash") == shadow.get("fallback_event_hash")
                and event.get("type")
                == "routing_host_fallback_execution_verified"
            ),
            None,
        )
        if fallback_event is None:
            errors.append(
                "routing host fallback execution event is absent from the signed run journal"
            )
        elif fallback_record is not None:
            expected_fallback_payload = {
                **fallback_record,
                "fallback_receipt_sha256": shadow.get("fallback_receipt_sha256"),
            }
            if fallback_event.get("payload") != expected_fallback_payload:
                errors.append(
                    "routing host fallback event payload differs from the receipt"
                )
            if not (
                isinstance(fallback_event.get("sequence"), int)
                and isinstance((approval_event or {}).get("sequence"), int)
                and fallback_event["sequence"] > approval_event["sequence"]
            ):
                errors.append(
                    "routing host fallback event does not follow the authorization receipt"
                )
            fallback_results = [
                item
                for item in (report or {}).get("results", [])
                if isinstance(item, dict) and item.get("decision") == "fallback"
            ]
            expected_fallback_ids = sorted(
                item.get("case_id")
                for item in fallback_results
                if isinstance(item.get("case_id"), str)
            )
            cases = fallback_record.get("cases")
            valid_cases = isinstance(cases, list) and all(
                isinstance(item, dict) and set(item) == FALLBACK_CASE_KEYS
                for item in cases
            )
            if not valid_cases:
                errors.append("routing fallback receipt cases are invalid")
                cases = []
            receipt_case_ids = [item.get("case_id") for item in cases]
            if (
                not all(
                    isinstance(item.get("case_id"), str)
                    and skill_router.OPAQUE_ID_RE.fullmatch(item["case_id"])
                    and item.get("router_decision") == "fallback"
                    and isinstance(item.get("baseline_selection_id"), str)
                    and skill_router.OPAQUE_ID_RE.fullmatch(
                        item["baseline_selection_id"]
                    )
                    and item.get("outcome") == "completed"
                    for item in cases
                )
                or len(receipt_case_ids) != len(set(receipt_case_ids))
            ):
                errors.append(
                    "routing fallback receipt lacks a completed baseline outcome per case"
                )
            if sorted(receipt_case_ids) != expected_fallback_ids:
                errors.append(
                    "routing fallback receipt case ids differ from routed fallback cases"
                )
            expected_fallback = {
                "schema_version": "skill-sublation.routing-fallback-execution-receipt.v1",
                "candidate_id": manifest.get("candidate_id"),
                "host_adapter_id": host_adapter_id,
                "host_adapter_sha256": shadow.get("host_adapter_sha256"),
                "index_sha256": shadow.get("index_sha256"),
                "case_id_set_sha256": (
                    report.get("case_id_set_sha256")
                    if isinstance(report, dict)
                    else None
                ),
                "fallback_case_id_set_sha256": skill_router.sha256(
                    expected_fallback_ids
                ),
                "fallback_case_count": len(expected_fallback_ids),
                "fallback_requested_count": len(expected_fallback_ids),
                "fallback_executed_count": len(expected_fallback_ids),
                "baseline_outcome_recorded_count": len(expected_fallback_ids),
                "execution_verified": True,
                "legacy_catalog_unchanged": True,
            }
            for key, expected in expected_fallback.items():
                if fallback_record.get(key) != expected:
                    errors.append(
                        f"routing fallback receipt binding mismatch: {key}"
                    )
            try:
                fallback_performed_time = _aware_utc(
                    fallback_record.get("performed_at")
                )
                fallback_event_time = _aware_utc(fallback_event.get("timestamp"))
                observed_last_time = _aware_utc(
                    (attestation or {}).get("last_ts")
                )
            except (TypeError, ValueError):
                errors.append("routing host fallback timestamp is invalid")
            else:
                if fallback_performed_time < observed_last_time:
                    errors.append(
                        "routing host fallback execution predates the observed evidence"
                    )
                if fallback_event_time < fallback_performed_time:
                    errors.append(
                        "routing host fallback journal event predates execution"
                    )
                if fallback_event_time > datetime.now(timezone.utc) + timedelta(
                    minutes=5
                ):
                    errors.append(
                        "routing host fallback event contains a future timestamp"
                    )

        rollback_receipt, rollback_path_errors = _external_path(
            shadow.get("rollback_receipt_path"),
            "validation.routing_shadow.rollback_receipt_path",
            kind="file",
        )
        errors.extend(rollback_path_errors)
        rollback_record: dict[str, Any] | None = None
        if rollback_receipt is not None:
            if authorization_run is None or not sublation_run.path_is_inside(
                rollback_receipt, authorization_run
            ):
                errors.append("routing rollback receipt must be inside the signed authorization run")
            if skill_router.sha256(rollback_receipt.read_bytes()) != shadow.get("rollback_receipt_sha256"):
                errors.append("routing rollback receipt sha256 mismatch")
            rollback_record = _load_report(
                rollback_receipt, "routing rollback receipt", errors
            )
            if rollback_record is not None and set(rollback_record) != ROLLBACK_RECEIPT_KEYS:
                errors.append("routing rollback receipt keys are invalid")
                rollback_record = None
        rollback_event = next(
            (
                event
                for event in journal
                if event.get("event_hash") == shadow.get("rollback_event_hash")
                and event.get("type") == "routing_registry_rollback_verified"
            ),
            None,
        )
        if rollback_event is None:
            errors.append("routing rollback event is absent from the signed run journal")
        elif rollback_record is not None:
            if not (
                isinstance(rollback_event.get("sequence"), int)
                and isinstance((fallback_event or {}).get("sequence"), int)
                and rollback_event["sequence"] > fallback_event["sequence"]
            ):
                errors.append(
                    "routing rollback event does not follow host fallback execution"
                )
            rollback_payload = rollback_event.get("payload")
            expected_payload = {
                **rollback_record,
                "rollback_receipt_sha256": shadow.get("rollback_receipt_sha256"),
            }
            if rollback_payload != expected_payload:
                errors.append("routing rollback event payload differs from the receipt")
            expected_rollback = {
                "schema_version": "skill-sublation.routing-rollback-receipt.v1",
                "candidate_id": manifest.get("candidate_id"),
                "index_shadow_sha256": shadow.get("index_sha256"),
                "index_sha256": shadow.get("index_sha256"),
                "router_sha256": shadow.get("router_sha256"),
                "profile_set_sha256": shadow.get("profile_set_sha256"),
                "action": "replace_then_restore",
                "fallback_verified": True,
                "registry_rollback_verified": True,
                "legacy_catalog_unchanged": True,
            }
            for key, expected in expected_rollback.items():
                if rollback_record.get(key) != expected:
                    errors.append(f"routing rollback receipt binding mismatch: {key}")
            if rollback_record.get("pre_tree_sha256") != rollback_record.get("post_rollback_tree_sha256"):
                errors.append("routing rollback did not restore the pre-shadow registry tree")
            if rollback_record.get("post_shadow_tree_sha256") == rollback_record.get("pre_tree_sha256"):
                errors.append("routing rollback drill did not produce an observable shadow-state change")
            if rollback_record.get("index_before_sha256") != rollback_record.get("index_after_sha256"):
                errors.append("routing rollback did not restore the prior index")
            registry_path, registry_errors = _external_path(
                rollback_record.get("registry_path"),
                "routing rollback registry_path",
                kind="directory",
            )
            errors.extend(registry_errors)
            if registry_path is not None:
                if sublation_run.tree_hash(registry_path) != rollback_record.get("post_rollback_tree_sha256"):
                    errors.append("routing rollback registry tree drifted after the drill")
                index_relative = rollback_record.get("index_relative_path")
                current_index, index_errors = _safe_relative_file(
                    registry_path,
                    index_relative,
                    "routing rollback index_relative_path",
                )
                errors.extend(index_errors)
                if current_index is not None:
                    current_index_payload = _load_report(
                        current_index, "routing rollback current index", errors
                    )
                    if current_index_payload is not None and current_index_payload.get("content_sha256") != rollback_record.get("index_after_sha256"):
                        errors.append("routing rollback current index differs from the restored index")
            try:
                rollback_performed_time = _aware_utc(
                    rollback_record.get("performed_at")
                )
                rollback_event_time = _aware_utc(rollback_event.get("timestamp"))
                observed_last_time = _aware_utc(
                    (attestation or {}).get("last_ts")
                )
            except (TypeError, ValueError):
                errors.append("routing rollback timestamp is invalid")
            else:
                if rollback_performed_time < observed_last_time:
                    errors.append("routing rollback drill predates the observed evidence")
                if rollback_event_time < rollback_performed_time:
                    errors.append("routing rollback journal event predates the drill")
                if rollback_event_time > datetime.now(timezone.utc) + timedelta(minutes=5):
                    errors.append("routing rollback event contains a future timestamp")

    for key in RATE_FIELDS:
        _number(shadow, key, errors, minimum=0, maximum=1)
    for key in COUNT_FIELDS:
        _number(shadow, key, errors, minimum=0, integer=True)
    samples = _number(shadow, "valid_samples", errors, minimum=30, integer=True)
    event_count = _number(
        shadow, "observation_event_count", errors, minimum=30, integer=True
    )
    _number(shadow, "p95_router_latency_ms", errors, minimum=0, maximum=1000)
    if report is not None and samples is not None and report.get("total") != samples:
        errors.append("routing fixture report total must equal valid_samples")
    if attestation is not None and event_count is not None and attestation.get("event_count") != event_count:
        errors.append("routing attestation event_count mismatch")
    if samples is not None and event_count is not None and samples > event_count:
        errors.append("valid_samples must not exceed verified observation_event_count")

    exact_one = (
        "explicit_invocation_adherence",
        "deterministic_order_rate",
        "fallback_success_rate",
    )
    for key in exact_one:
        if shadow.get(key) != 1.0:
            errors.append(f"validation.routing_shadow.{key} must equal 1.0")
    for key in COUNT_FIELDS:
        if shadow.get(key) != 0:
            errors.append(f"validation.routing_shadow.{key} must equal 0")
    single_top1 = shadow.get("single_domain_top1")
    if _finite_numeric(single_top1) and single_top1 < 0.90:
        errors.append("validation.routing_shadow.single_domain_top1 must be at least 0.90")
    multi_top3 = shadow.get("multi_domain_top3")
    if _finite_numeric(multi_top3) and multi_top3 < 0.95:
        errors.append("validation.routing_shadow.multi_domain_top3 must be at least 0.95")
    accuracy = shadow.get("route_accuracy")
    baseline_accuracy = shadow.get("baseline_route_accuracy")
    if all(
        _finite_numeric(item)
        for item in (accuracy, baseline_accuracy)
    ) and accuracy < baseline_accuracy:
        errors.append("routing route_accuracy regressed below baseline")
    correction = shadow.get("manual_correction_rate")
    baseline_correction = shadow.get("baseline_manual_correction_rate")
    if all(
        _finite_numeric(item)
        for item in (correction, baseline_correction)
    ) and correction > baseline_correction:
        errors.append("routing manual_correction_rate regressed above baseline")

    if shadow.get("net_token_savings_status") not in {"positive", "not_measurable"}:
        errors.append("routing net_token_savings_status must be positive or not_measurable")
    if shadow.get("net_token_savings_status") == "positive" and shadow.get("host_deferred_loading_verified") is not True:
        errors.append("positive token savings requires host_deferred_loading_verified=true")
    if shadow.get("fallback_verified") is not True or shadow.get("registry_rollback_verified") is not True:
        errors.append("routing fallback and registry rollback must both be verified")

    reviews = shadow.get("independent_reviews")
    builder = str(manifest.get("created_by") or "").strip().casefold()
    if not isinstance(reviews, list):
        errors.append("routing observation closure requires independent_reviews")
    else:
        reviewers: set[str] = set()
        message_ids: set[str] = set()
        artifact_binding = skill_router.sha256(
            {
                "candidate_id": manifest.get("candidate_id"),
                "candidate_tree_hash": (
                    promotion_event.get("payload", {}).get("candidate_tree_hash")
                    if isinstance(promotion_event, dict)
                    else None
                ),
                "index_sha256": shadow.get("index_sha256"),
                "taxonomy_sha256": (
                    index.get("taxonomy_sha256") if isinstance(index, dict) else None
                ),
                "router_sha256": shadow.get("router_sha256"),
                "profile_set_sha256": shadow.get("profile_set_sha256"),
                "fixture_plan_sha256": shadow.get("fixture_plan_sha256"),
                "fixture_plan_artifact_sha256": shadow.get(
                    "fixture_plan_artifact_sha256"
                ),
                "fixture_report_sha256": shadow.get("fixture_report_sha256"),
                "observation_log_sha256": shadow.get("observation_log_sha256"),
                "attestation_report_sha256": shadow.get("attestation_report_sha256"),
                "case_id_set_sha256": (
                    report.get("case_id_set_sha256")
                    if isinstance(report, dict)
                    else None
                ),
                "authorization_receipt_sha256": shadow.get(
                    "authorization_receipt_sha256"
                ),
                "promotion_event_hash": shadow.get("promotion_event_hash"),
                "host_adapter_id": shadow.get("host_adapter_id"),
                "host_adapter_sha256": shadow.get("host_adapter_sha256"),
                "fallback_receipt_sha256": shadow.get(
                    "fallback_receipt_sha256"
                ),
                "fallback_event_hash": shadow.get("fallback_event_hash"),
                "rollback_receipt_sha256": shadow.get("rollback_receipt_sha256"),
                "rollback_event_hash": shadow.get("rollback_event_hash"),
                "attestation_key_id": shadow.get("attestation_key_id"),
                "attestation_key_sha256": shadow.get("attestation_key_sha256"),
            }
        )
        for position, review in enumerate(reviews):
            if not isinstance(review, dict) or set(review) != INDEPENDENT_REVIEW_KEYS:
                errors.append(f"independent_reviews[{position}] keys are invalid")
                continue
            reviewer = review.get("reviewer")
            message_id = review.get("message_id")
            normalized_reviewer = (
                reviewer.strip().casefold() if isinstance(reviewer, str) else ""
            )
            if (
                not isinstance(reviewer, str)
                or not skill_router.OPAQUE_ID_RE.fullmatch(reviewer)
                or reviewer != normalized_reviewer
                or normalized_reviewer == builder
            ):
                errors.append(f"independent_reviews[{position}].reviewer must be independent from builder")
            else:
                reviewers.add(normalized_reviewer)
            if not isinstance(message_id, str) or not skill_router.OPAQUE_ID_RE.fullmatch(message_id):
                errors.append(f"independent_reviews[{position}].message_id is required")
            else:
                message_ids.add(message_id)
            if review.get("decision") != "approve":
                errors.append(f"independent_reviews[{position}].decision must be approve")
            if review.get("artifact_binding_sha256") != artifact_binding:
                errors.append(f"independent_reviews[{position}] artifact binding mismatch")
            room_log, room_errors = _external_path(
                review.get("room_log_path"),
                f"independent_reviews[{position}].room_log_path",
                kind="file",
            )
            errors.extend(room_errors)
            if room_log is None or not isinstance(message_id, str):
                continue
            matched_events: list[dict[str, Any]] = []
            try:
                for line_number, line in enumerate(
                    room_log.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError(f"line {line_number} is not an object")
                    if event.get("id") == message_id:
                        matched_events.append(event)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"independent_reviews[{position}] room evidence is invalid: {exc}")
                continue
            if len(matched_events) != 1:
                errors.append(f"independent_reviews[{position}] message id is not unique in the room log")
                continue
            message = matched_events[0]
            if skill_router.sha256(message) != review.get("message_sha256"):
                errors.append(f"independent_reviews[{position}] room message sha256 mismatch")
            if message.get("speaker") != reviewer:
                errors.append(f"independent_reviews[{position}] room speaker mismatch")
            text = message.get("text")
            review_marker = (
                f"[routing-review decision=approve artifact={artifact_binding}]"
            )
            if not isinstance(text, str) or review_marker not in text:
                errors.append(f"independent_reviews[{position}] message lacks an approve artifact binding")
            try:
                reviewed_time = _aware_utc(message.get("timestamp"))
                observed_time = _aware_utc((attestation or {}).get("last_ts"))
                rollback_time = _aware_utc(
                    (rollback_event or {}).get("timestamp")
                )
            except (TypeError, ValueError):
                errors.append(f"independent_reviews[{position}] timestamp is invalid")
            else:
                if reviewed_time < observed_time:
                    errors.append(f"independent_reviews[{position}] predates the reviewed evidence")
                if reviewed_time <= rollback_time:
                    errors.append(
                        f"independent_reviews[{position}] predates the rollback evidence"
                    )
                if reviewed_time > datetime.now(timezone.utc) + timedelta(minutes=5):
                    errors.append(
                        f"independent_reviews[{position}] contains a future timestamp"
                    )
        if len(reviewers) < 2 or len(message_ids) < 2:
            errors.append("routing observation closure requires two distinct reviewers and message ids")
    return errors


def promotion_errors(root: Path, manifest: dict[str, Any]) -> list[str]:
    errors, _warnings = metadata_findings(root, manifest)
    routing = manifest.get("routing")
    if not isinstance(routing, dict):
        errors.append("promotion requires routing metadata under the current policy")
    elif routing.get("required_on_promotion") is True:
        if routing.get("attestation_key_id") is None or routing.get("attestation_key_sha256") is None:
            errors.append("promotion requires a precommitted routing attestation key id and hash")
        if routing.get("classification_status") != "approved":
            errors.append("promotion requires routing.classification_status=approved")
        if routing.get("index_mode") != "shadow":
            errors.append("first routing promotion must remain in shadow mode")
    return errors
