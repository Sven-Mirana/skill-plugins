#!/usr/bin/env python3
"""Default-off host-shadow adapter for privacy-bounded Skill advice.

The adapter never accepts a raw prompt.  It accepts either closed-vocabulary
feature IDs or a case ID from the frozen synthetic fixture.  Its recommendation
is advisory and the host's effective selection is always preserved.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import skill_router


CONFIG_SCHEMA_VERSION = "skill-sublation.host-shadow-config.v1"
PACKET_SCHEMA_VERSION = "skill-sublation.host-shadow-packet.v1"
OUTCOME_SCHEMA_VERSION = "skill-sublation.host-shadow-outcome.v1"
REPORT_SCHEMA_VERSION = "skill-sublation.host-shadow-report.v1"
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PACKET_KEYS = {"schema_version", "request_id", "sample_source", "fixture_case_id", "features"}
FEATURE_KEYS = {"explicit_skills", "positive_triggers", "negative_triggers", "domains", "capabilities"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"JSON input must be a regular file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load an explicit config; absence is always disabled."""

    raw: Any = (
        read_json(path)
        if path is not None
        else {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "enabled": False,
            "mode": "host_shadow",
            "advisory_only": True,
            "preserve_host_selection": True,
            "allow_automatic_execution": False,
            "allow_live_routing": False,
            "input_policy": "redacted_closed_vocabulary_or_frozen_synthetic_fixture_only",
            "net_token_savings_status": "not_measurable",
            "host_deferred_loading_verified": False,
        }
    )
    if not isinstance(raw, dict):
        raise ValueError("host-shadow config must be an object")
    required = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "mode": "host_shadow",
        "advisory_only": True,
        "preserve_host_selection": True,
        "allow_automatic_execution": False,
        "allow_live_routing": False,
        "input_policy": "redacted_closed_vocabulary_or_frozen_synthetic_fixture_only",
        "net_token_savings_status": "not_measurable",
        "host_deferred_loading_verified": False,
    }
    for key, expected in required.items():
        if raw.get(key) != expected:
            raise ValueError(f"host-shadow config {key} must be {expected!r}")
    if not isinstance(raw.get("enabled"), bool):
        raise ValueError("host-shadow config enabled must be boolean")
    return raw


def _feature_catalog(index: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, str]:
    """Build an opaque, closed vocabulary from the already-reviewed index."""

    catalog: dict[str, str] = {}
    for entry in index.get("entries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
            continue
        metadata = entry["metadata"]
        runtime_id = str(entry.get("runtime_id"))
        token = str(entry.get("metadata_sha256", "sha256:unknown")).split(":")[-1][:12]
        catalog[f"skill:{runtime_id}"] = f"${metadata['skill']}"
        for position, value in enumerate(metadata.get("triggers", [])):
            catalog[f"trigger:{token}:{position}"] = str(value)
        for position, value in enumerate(metadata.get("negative_triggers", [])):
            catalog[f"negative:{token}:{position}"] = str(value)
    for item in taxonomy.get("domains", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            aliases = item.get("aliases", [])
            if aliases:
                catalog[f"domain:{item['id']}"] = str(aliases[0])
    for item in taxonomy.get("capabilities", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            aliases = item.get("aliases", [])
            if aliases:
                catalog[f"capability:{item['id']}"] = str(aliases[0])
    return catalog


def _validate_host_selection(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and OPAQUE_ID_RE.fullmatch(item) for item in value):
        raise ValueError("host selection must be an array of opaque identifiers")
    return list(value)


def _fixture_prompt(packet: dict[str, Any], fixture: dict[str, Any] | None) -> str:
    if fixture is None or fixture.get("sample_source") != "synthetic_fixture":
        raise ValueError("synthetic fixture input requires the frozen synthetic fixture")
    case_id = packet.get("fixture_case_id")
    if not isinstance(case_id, str) or not OPAQUE_ID_RE.fullmatch(case_id):
        raise ValueError("synthetic fixture input requires an opaque fixture_case_id")
    matches = [case for case in fixture.get("cases", []) if isinstance(case, dict) and case.get("id") == case_id]
    if len(matches) != 1 or not isinstance(matches[0].get("prompt"), str):
        raise ValueError("fixture_case_id is absent or ambiguous")
    return matches[0]["prompt"]


def _redacted_feature_prompt(
    packet: dict[str, Any],
    index: dict[str, Any],
    taxonomy: dict[str, Any],
) -> str:
    features = packet.get("features")
    if not isinstance(features, dict) or set(features) - FEATURE_KEYS:
        raise ValueError("features must contain only closed-vocabulary feature groups")
    catalog = _feature_catalog(index, taxonomy)
    resolved: list[str] = []
    seen: set[str] = set()
    for key in sorted(FEATURE_KEYS):
        values = features.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"features.{key} must be a string array")
        for feature_id in values:
            if feature_id in seen:
                continue
            if feature_id not in catalog:
                raise ValueError(f"unknown or unreviewed feature id: {feature_id}")
            expected_prefix = {
                "explicit_skills": "skill:",
                "positive_triggers": "trigger:",
                "negative_triggers": "negative:",
                "domains": "domain:",
                "capabilities": "capability:",
            }[key]
            if not feature_id.startswith(expected_prefix):
                raise ValueError(f"feature id is in the wrong group: {feature_id}")
            seen.add(feature_id)
            resolved.append(catalog[feature_id])
    if not resolved:
        return "__no_reviewed_features__"
    return "；".join(resolved)


def _resolve_packet(
    packet: Any,
    index: dict[str, Any],
    taxonomy: dict[str, Any],
    fixture: dict[str, Any] | None,
) -> tuple[str, str]:
    if not isinstance(packet, dict):
        raise ValueError("host-shadow packet must be an object")
    if set(packet) - PACKET_KEYS:
        raise ValueError("host-shadow packet contains unsupported keys")
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise ValueError("host-shadow packet schema_version is invalid")
    request_id = packet.get("request_id")
    if not isinstance(request_id, str) or not OPAQUE_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must be opaque")
    source = packet.get("sample_source")
    if source == "redacted_features":
        if "fixture_case_id" in packet:
            raise ValueError("redacted feature packet cannot select a fixture case")
        return request_id, _redacted_feature_prompt(packet, index, taxonomy)
    if source == "synthetic_fixture":
        if "features" in packet:
            raise ValueError("synthetic fixture packet cannot contain features")
        return request_id, _fixture_prompt(packet, fixture)
    raise ValueError("sample_source must be redacted_features or synthetic_fixture")


def _project_advice(route: dict[str, Any]) -> dict[str, Any]:
    recommended = route.get("recommended", [])
    top = recommended[0] if recommended else None
    top_score = float(top.get("score", 0.0)) if isinstance(top, dict) else 0.0
    confidence = round(top_score / (top_score + 8.0), 4) if top_score > 0 else 0.0
    return {
        "decision": route.get("decision"),
        "category": top.get("primary_domain") if isinstance(top, dict) else None,
        "skill": top.get("skill") if isinstance(top, dict) else None,
        "runtime_id": top.get("runtime_id") if isinstance(top, dict) else None,
        "confidence": confidence,
        "confidence_semantics": "deterministic_score_ratio_not_probability",
        "reason_codes": sorted(
            set(route.get("reason_codes", []))
            | set(top.get("reason_codes", []) if isinstance(top, dict) else [])
        ),
        "fallback_used": bool(route.get("fallback_used")),
        "clarification_required": bool(route.get("clarification_required")),
        "safety_gate_required": bool(route.get("safety_gate_required")),
        "runtime_binding_verified": route.get("runtime_binding_verified"),
    }


def observe(
    packet: Any,
    host_selection: Any,
    index: dict[str, Any],
    taxonomy: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    fixture: dict[str, Any] | None = None,
    runtime_roots: Iterable[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    """Return advisory shadow output while preserving the host decision."""

    started = time.perf_counter_ns()
    selected = _validate_host_selection(host_selection)
    effective_config = config or load_config()
    if not effective_config["enabled"]:
        return {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "status": "disabled_noop",
            "request_id": None,
            "host_selection": selected,
            "effective_selection": list(selected),
            "shadow_advice": None,
            "advisory_only": True,
            "execution_performed": False,
            "authority_granted": False,
            "legacy_router_changed": False,
            "raw_prompt_accepted": False,
            "overhead": {
                "adapter_latency_ms": round((time.perf_counter_ns() - started) / 1_000_000, 6),
                "input_bytes": 0,
                "index_bytes": 0,
                "output_projection_bytes": 0,
                "net_token_savings_status": "not_measurable",
                "host_deferred_loading_verified": False,
            },
        }

    request_id, synthetic_prompt = _resolve_packet(packet, index, taxonomy, fixture)
    route = skill_router.route_prompt(
        synthetic_prompt,
        index,
        taxonomy,
        runtime_roots=runtime_roots,
    )
    advice = _project_advice(route)
    projection = {
        "request_id": request_id,
        "host_selection": selected,
        "effective_selection": selected,
        "shadow_advice": advice,
    }
    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "status": "observed",
        **projection,
        "advisory_only": True,
        "execution_performed": False,
        "authority_granted": False,
        "legacy_router_changed": False,
        "raw_prompt_accepted": False,
        "overhead": {
            "adapter_latency_ms": round((time.perf_counter_ns() - started) / 1_000_000, 6),
            "input_bytes": len(canonical_bytes(packet)),
            "index_bytes": len(canonical_bytes(index)),
            "output_projection_bytes": len(canonical_bytes(projection)),
            "net_token_savings_status": "not_measurable",
            "host_deferred_loading_verified": False,
        },
    }


def evaluate_synthetic(
    fixture: dict[str, Any],
    index: dict[str, Any],
    taxonomy: dict[str, Any],
    runtime_roots: Iterable[tuple[str, Path]],
) -> dict[str, Any]:
    """Exercise the adapter with exactly the existing synthetic fixture set."""

    if fixture.get("sample_source") != "synthetic_fixture":
        raise ValueError("host-shadow evaluation is limited to synthetic_fixture")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or len(cases) != 39:
        raise ValueError("host-shadow evaluation requires the frozen 39-case fixture")
    enabled = {**load_config(), "enabled": True}
    roots = list(runtime_roots)
    if not roots:
        raise ValueError("host-shadow evaluation requires runtime roots")
    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = case.get("id") if isinstance(case, dict) else None
        if not isinstance(case_id, str):
            raise ValueError("synthetic fixture case id is invalid")
        baseline = [f"legacy:{case_id}"]
        packet = {
            "schema_version": PACKET_SCHEMA_VERSION,
            "request_id": f"synthetic:{case_id}",
            "sample_source": "synthetic_fixture",
            "fixture_case_id": case_id,
        }
        if case.get("case_type") == "tree_drift":
            root_id, original_root = roots[0]
            with tempfile.TemporaryDirectory(prefix="host-shadow-drift-") as temporary:
                drift_root = Path(temporary) / "runtime-root"
                shutil.copytree(original_root, drift_root)
                regular_skills = sorted(drift_root.rglob("SKILL.md"))
                if not regular_skills:
                    raise ValueError("tree-drift fixture cannot locate a regular SKILL.md")
                with regular_skills[0].open("a", encoding="utf-8") as handle:
                    handle.write("\n# isolated host-shadow tree drift\n")
                case_roots = [(root_id, drift_root), *roots[1:]]
                outcome = observe(
                    packet,
                    baseline,
                    index,
                    taxonomy,
                    config=enabled,
                    fixture=fixture,
                    runtime_roots=case_roots,
                )
        else:
            outcome = observe(
                packet,
                baseline,
                index,
                taxonomy,
                config=enabled,
                fixture=fixture,
                runtime_roots=roots,
            )
        advice = outcome["shadow_advice"]
        expected_top = case.get("expected_top")
        expected_decision = case.get("expected_decision")
        top_ok = expected_top is None or advice["runtime_id"] == expected_top
        decision_ok = advice["decision"] == expected_decision
        preserved = outcome["effective_selection"] == baseline
        safe = (
            outcome["advisory_only"]
            and not outcome["execution_performed"]
            and not outcome["authority_granted"]
            and not outcome["legacy_router_changed"]
            and not outcome["raw_prompt_accepted"]
        )
        results.append(
            {
                "case_id": case_id,
                "passed": bool(top_ok and decision_ok and preserved and safe),
                "decision": advice["decision"],
                "category": advice["category"],
                "skill": advice["skill"],
                "confidence": advice["confidence"],
                "reason_codes": advice["reason_codes"],
                "host_selection_preserved": preserved,
                "adapter_latency_ms": outcome["overhead"]["adapter_latency_ms"],
                "input_bytes": outcome["overhead"]["input_bytes"],
                "output_projection_bytes": outcome["overhead"]["output_projection_bytes"],
            }
        )
    latencies = sorted(float(item["adapter_latency_ms"]) for item in results)
    p95_position = max(0, int(0.95 * len(latencies) + 0.999999) - 1)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": fixture.get("fixture_id"),
        "sample_source": "synthetic_fixture",
        "configuration": {
            "artifact_default_enabled": False,
            "authorized_test_enabled": True,
            "advisory_only": True,
            "preserve_host_selection": True,
            "automatic_execution": False,
            "live_routing": False,
        },
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "host_selection_preserved": all(item["host_selection_preserved"] for item in results),
        "p95_adapter_latency_ms": latencies[p95_position],
        "mean_input_bytes": round(sum(int(item["input_bytes"]) for item in results) / len(results), 3),
        "mean_output_projection_bytes": round(
            sum(int(item["output_projection_bytes"]) for item in results) / len(results), 3
        ),
        "index_bytes": len(canonical_bytes(index)),
        "net_token_savings_status": "not_measurable",
        "host_deferred_loading_verified": False,
        "raw_prompt_stored": False,
        "results": results,
    }


def parse_root_spec(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("skills root must be ROOT_ID=PATH")
    root_id, raw_path = value.split("=", 1)
    if not skill_router.ROOT_ID_RE.fullmatch(root_id):
        raise argparse.ArgumentTypeError("skills root id is invalid")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError("skills root path is unavailable")
    return root_id, path


def write_json(path: Path | None, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    observe_parser = sub.add_parser("observe")
    observe_parser.add_argument("--packet", required=True, type=Path)
    observe_parser.add_argument("--host-selection", required=True, type=Path)
    observe_parser.add_argument("--index", required=True, type=Path)
    observe_parser.add_argument("--config", type=Path)
    observe_parser.add_argument("--fixture", type=Path)
    observe_parser.add_argument("--skills-root", action="append", default=[], type=parse_root_spec)
    observe_parser.add_argument("--output", type=Path)

    evaluate_parser = sub.add_parser("evaluate-synthetic")
    evaluate_parser.add_argument("--fixture", required=True, type=Path)
    evaluate_parser.add_argument("--index", required=True, type=Path)
    evaluate_parser.add_argument("--skills-root", action="append", required=True, type=parse_root_spec)
    evaluate_parser.add_argument("--authorized-test", action="store_true")
    evaluate_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    try:
        index = read_json(args.index)
        taxonomy = skill_router.load_taxonomy()
        if args.command == "observe":
            outcome = observe(
                read_json(args.packet),
                read_json(args.host_selection),
                index,
                taxonomy,
                config=load_config(args.config),
                fixture=read_json(args.fixture) if args.fixture else None,
                runtime_roots=args.skills_root or None,
            )
            write_json(args.output, outcome)
            return 0
        if not args.authorized_test:
            raise ValueError("synthetic host-shadow evaluation requires --authorized-test")
        report = evaluate_synthetic(
            read_json(args.fixture),
            index,
            taxonomy,
            args.skills_root,
        )
        write_json(args.output, report)
        return 0 if report["failed"] == 0 else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"host-shadow error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
