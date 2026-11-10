#!/usr/bin/env python3
"""Build a derived Skill index and run privacy-preserving shadow routing."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TAXONOMY_SCHEMA_VERSION = "skill-sublation.routing-taxonomy.v1"
METADATA_SCHEMA_VERSION = "skill-sublation.routing-metadata.v1"
INDEX_SCHEMA_VERSION = "skill-sublation.routing-index.v1"
ROUTE_SCHEMA_VERSION = "skill-sublation.route-result.v1"
OBSERVATION_SCHEMA_VERSION = "skill-sublation.routing-observation.v1"
OBSERVATION_VERIFICATION_SCHEMA_VERSION = "skill-sublation.routing-observation-verification.v1"
TREE_HASH_ALGORITHM = "sublation-canonical-tree-v1"
SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
OBSERVATION_BASE_KEYS = {
    "schema_version",
    "ts",
    "request_id",
    "evaluation_cohort",
    "prompt_fingerprint",
    "deterministic_replay_verified",
    "mode",
    "index_sha256",
    "router_sha256",
    "profile_set_sha256",
    "decision",
    "recommended_runtime_ids",
    "fallback_used",
    "clarification_required",
    "safety_gate_required",
    "reason_codes",
    "latency_ms",
    "baseline_skill",
    "authority_granted",
    "raw_prompt_stored",
    "runtime_binding_verified",
}
ROUTING_METRIC_KEYS = {
    "explicit_invocation_adherence",
    "deterministic_order_rate",
    "fallback_success_rate",
    "negative_control_misroutes",
    "high_risk_misroutes",
    "authority_expansion_events",
    "raw_prompt_leaks",
    "tree_drift_misses",
    "valid_samples",
    "single_domain_top1",
    "multi_domain_top3",
    "route_accuracy",
    "baseline_route_accuracy",
    "manual_correction_rate",
    "baseline_manual_correction_rate",
    "p95_router_latency_ms",
    "net_token_savings_status",
    "host_deferred_loading_verified",
    "fallback_verified",
    "registry_rollback_verified",
}
FIXTURE_CASE_TYPES = {
    "general",
    "single_domain",
    "multi_domain",
    "explicit",
    "fallback",
    "negative_control",
    "high_risk",
    "tree_drift",
}
FIXTURE_FAILURE_CODES = {
    "decision_mismatch",
    "top_mismatch",
    "missing_expected",
    "forbidden_present",
    "raw_prompt_leak",
    "safety_gate_mismatch",
    "authority_expansion",
    "nondeterministic_result",
}
EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "candidates",
    "fixtures",
    "node_modules",
    "rollback-points",
    "validation",
}
METADATA_KEYS = {
    "schema_version",
    "skill",
    "routing_metadata_version",
    "primary_domain",
    "capabilities",
    "triggers",
    "negative_triggers",
    "input_types",
    "output_types",
    "dependencies",
    "risk_tier",
    "allowed_actions",
    "prohibited_actions",
    "clarify_when",
    "fallback_mode",
    "classification",
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def read_existing_plan(path: Path) -> dict[str, Any]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("supplied fixture plan must be a regular non-symlink file")
    return read_json(path)


def paths_alias(first: Path | None, second: Path | None) -> bool:
    if first is None or second is None:
        return False
    try:
        if first.exists() and second.exists() and os.path.samefile(first, second):
            return True
        return first.resolve(strict=False) == second.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("artifact paths could not be resolved safely") from exc


def write_json(path: Path | None, value: dict[str, Any]) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).lower().split())


def _string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(value) == len(set(value))
    )


def default_taxonomy_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "skill-routing-taxonomy-v1.json"


def validate_taxonomy(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["taxonomy must be a JSON object"]
    errors: list[str] = []
    if value.get("schema_version") != TAXONOMY_SCHEMA_VERSION:
        errors.append(f"taxonomy.schema_version must be {TAXONOMY_SCHEMA_VERSION}")
    for key in ("domains", "capabilities"):
        entries = value.get(key)
        if not isinstance(entries, list) or not entries:
            errors.append(f"taxonomy.{key} must be a nonempty array")
            continue
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            prefix = f"taxonomy.{key}[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{prefix} must be an object")
                continue
            item_id = entry.get("id")
            if not isinstance(item_id, str) or not ROOT_ID_RE.fullmatch(item_id):
                errors.append(f"{prefix}.id must be a canonical id")
            elif item_id in seen:
                errors.append(f"duplicate {key} id: {item_id}")
            else:
                seen.add(item_id)
            if not _string_list(entry.get("aliases", [])):
                errors.append(f"{prefix}.aliases must be a unique string array")
    thresholds = value.get("thresholds")
    if not isinstance(thresholds, dict):
        errors.append("taxonomy.thresholds must be an object")
    else:
        for key in ("minimum_score", "ambiguous_margin", "cross_domain_ratio"):
            if not isinstance(thresholds.get(key), (int, float)):
                errors.append(f"taxonomy.thresholds.{key} must be numeric")
        for key in ("default_top_k", "max_composition"):
            if not isinstance(thresholds.get(key), int) or thresholds.get(key, 0) < 1:
                errors.append(f"taxonomy.thresholds.{key} must be a positive integer")
    return errors


def load_taxonomy(path: Path | None = None) -> dict[str, Any]:
    value = read_json(path or default_taxonomy_path())
    errors = validate_taxonomy(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def taxonomy_ids(taxonomy: dict[str, Any], key: str) -> set[str]:
    return {str(entry["id"]) for entry in taxonomy[key]}


def validate_metadata(value: Any, taxonomy: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["routing metadata must be a JSON object"]
    errors: list[str] = []
    unknown = sorted(set(value) - METADATA_KEYS)
    missing = sorted(METADATA_KEYS - set(value))
    if unknown:
        errors.append("unsupported metadata keys: " + ", ".join(unknown))
    if missing:
        errors.append("missing metadata keys: " + ", ".join(missing))
    if value.get("schema_version") != METADATA_SCHEMA_VERSION:
        errors.append(f"schema_version must be {METADATA_SCHEMA_VERSION}")
    skill = value.get("skill")
    if not isinstance(skill, str) or not SKILL_RE.fullmatch(skill):
        errors.append("skill must be a canonical lowercase id")
    if not isinstance(value.get("routing_metadata_version"), int) or value.get("routing_metadata_version", 0) < 1:
        errors.append("routing_metadata_version must be a positive integer")
    if value.get("primary_domain") not in taxonomy_ids(taxonomy, "domains"):
        errors.append("primary_domain is not in the controlled taxonomy")
    capabilities = value.get("capabilities")
    if not _string_list(capabilities, nonempty=True):
        errors.append("capabilities must be a nonempty unique string array")
    else:
        unknown_capabilities = sorted(set(capabilities) - taxonomy_ids(taxonomy, "capabilities"))
        if unknown_capabilities:
            errors.append("unknown capabilities: " + ", ".join(unknown_capabilities))
    for key, nonempty in (
        ("triggers", True),
        ("negative_triggers", False),
        ("input_types", True),
        ("output_types", True),
        ("dependencies", False),
        ("allowed_actions", True),
        ("prohibited_actions", True),
        ("clarify_when", False),
    ):
        if not _string_list(value.get(key), nonempty=nonempty):
            errors.append(f"{key} must be a {'nonempty ' if nonempty else ''}unique string array")
    triggers = {normalize(item) for item in value.get("triggers", []) if isinstance(item, str)}
    negatives = {normalize(item) for item in value.get("negative_triggers", []) if isinstance(item, str)}
    overlap = sorted(triggers & negatives)
    if overlap:
        errors.append("triggers and negative_triggers overlap: " + ", ".join(overlap))
    if value.get("risk_tier") not in {"low", "medium", "high"}:
        errors.append("risk_tier must be low, medium, or high")
    if value.get("fallback_mode") != "legacy_catalog":
        errors.append("fallback_mode must be legacy_catalog")
    classification = value.get("classification")
    if not isinstance(classification, dict):
        errors.append("classification must be an object")
    else:
        if set(classification) != {"method", "status", "evidence"}:
            errors.append("classification must contain only method, status, evidence")
        if classification.get("method") not in {
            "deterministic_suggestion",
            "human_reviewed",
            "imported_reviewed",
        }:
            errors.append("classification.method is invalid")
        status_value = classification.get("status")
        if status_value not in {"suggested", "candidate", "approved", "rejected"}:
            errors.append("classification.status is invalid")
        if not _string_list(classification.get("evidence"), nonempty=True):
            errors.append("classification.evidence must be nonempty")
        if status_value == "approved" and not value.get("negative_triggers"):
            errors.append("approved metadata requires at least one negative trigger")
        if status_value == "approved" and classification.get("method") not in {
            "human_reviewed",
            "imported_reviewed",
        }:
            errors.append("approved metadata requires a human-reviewed classification method")
    return errors


def metadata_sha256(value: dict[str, Any]) -> str:
    return sha256(value)


def _ignored_tree_path(relative: Path) -> bool:
    return (
        any(part in {".git", "__pycache__"} for part in relative.parts)
        or relative.suffix in {".pyc", ".pyo"}
        or relative.name == ".DS_Store"
    )


def collect_tree_hashes(root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    root = root.expanduser().absolute()
    if not root.exists() and not root.is_symlink():
        return {".": {"kind": "missing"}}
    root_metadata = os.lstat(root)
    root_mode = stat.S_IMODE(root_metadata.st_mode)
    if stat.S_ISLNK(root_metadata.st_mode):
        return {".": {"kind": "symlink", "mode": root_mode, "target": os.readlink(root)}}
    if stat.S_ISREG(root_metadata.st_mode):
        return {".": {"kind": "file", "mode": root_mode, "sha256": sha256(root.read_bytes())}}
    if not stat.S_ISDIR(root_metadata.st_mode):
        return {".": {"kind": "other", "mode": root_mode, "device": int(root_metadata.st_rdev)}}
    entries["./"] = {"kind": "directory", "mode": root_mode}
    root = root.resolve()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        retained: list[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            relative = path.relative_to(root)
            if _ignored_tree_path(relative):
                continue
            metadata = os.lstat(path)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                entries[relative.as_posix()] = {"kind": "symlink", "mode": mode, "target": os.readlink(path)}
            else:
                entries[relative.as_posix() + "/"] = {"kind": "directory", "mode": mode}
                retained.append(name)
        dirnames[:] = retained
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(root)
            if _ignored_tree_path(relative):
                continue
            metadata = os.lstat(path)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                entry = {"kind": "symlink", "mode": mode, "target": os.readlink(path)}
            elif stat.S_ISREG(metadata.st_mode):
                entry = {"kind": "file", "mode": mode, "sha256": sha256(path.read_bytes())}
            else:
                entry = {"kind": "other", "mode": mode, "device": int(metadata.st_rdev)}
            entries[relative.as_posix()] = entry
    return entries


def tree_hash(root: Path) -> str:
    return sha256(collect_tree_hashes(root))


def parse_root_spec(value: str) -> tuple[str, Path]:
    if "=" in value:
        root_id, path_text = value.split("=", 1)
    else:
        path_text = value
        root_id = re.sub(r"[^a-z0-9._-]+", "-", Path(value).expanduser().name.lower()).strip("-")
    if not ROOT_ID_RE.fullmatch(root_id):
        raise ValueError(f"invalid root id: {root_id!r}")
    path = Path(path_text).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"skills root is not a directory: {path}")
    return root_id, path


def discover_metadata(roots: Iterable[tuple[str, Path]]) -> list[tuple[str, Path, Path]]:
    discovered: list[tuple[str, Path, Path]] = []
    for root_id, root in roots:
        for path in sorted(root.rglob("routing.json")):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.is_file() and not path.is_symlink():
                discovered.append((root_id, root, path))
    return discovered


def _profile_entry(
    root_id: str,
    root: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    skill_root = metadata_path.parent.resolve()
    relative = skill_root.relative_to(root).as_posix()
    runtime_suffix = metadata["skill"] if relative == "." else relative
    runtime_id = f"{root_id}:{runtime_suffix}"
    return {
        "runtime_id": runtime_id,
        "root_id": root_id,
        "relative_path": relative,
        "declared_name": metadata["skill"],
        "skill_tree_hash": tree_hash(skill_root),
        "tree_hash_algorithm": TREE_HASH_ALGORITHM,
        "metadata_sha256": metadata_sha256(metadata),
        "metadata": metadata,
    }


def build_index(
    root_specs: Iterable[tuple[str, Path]],
    metadata_paths: Iterable[Path],
    taxonomy: dict[str, Any],
    *,
    mode: str,
    router_sha256: str,
    previous_index_sha256: str | None = None,
) -> dict[str, Any]:
    if mode not in {"shadow", "live"}:
        raise ValueError("index mode must be shadow or live")
    roots = list(root_specs)
    seen_root_ids: set[str] = set()
    for root_id, _root in roots:
        if root_id in seen_root_ids:
            raise ValueError(f"duplicate root id: {root_id}")
        seen_root_ids.add(root_id)
    sources = discover_metadata(roots)
    for path in metadata_paths:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"metadata path is not a file: {resolved}")
        synthetic_root = resolved.parent
        # Keep an explicitly supplied metadata identity stable across argument
        # ordering without disclosing its absolute path in the index.
        synthetic_id = "file-" + hashlib.sha256(
            resolved.as_posix().encode("utf-8")
        ).hexdigest()[:12]
        sources.append((synthetic_id, synthetic_root, resolved))

    entries_by_runtime: dict[str, dict[str, Any]] = {}
    for root_id, root, path in sources:
        metadata = read_json(path)
        errors = validate_metadata(metadata, taxonomy)
        skill_path = path.parent / "SKILL.md"
        if not skill_path.is_file() or skill_path.is_symlink():
            errors.append("routing.json must have a regular sibling SKILL.md")
        else:
            declared_name = _frontmatter(skill_path).get("name")
            if declared_name != metadata.get("skill"):
                errors.append("SKILL.md frontmatter name must match routing metadata skill")
        if errors:
            raise ValueError(f"{path}: " + "; ".join(errors))
        status_value = metadata["classification"]["status"]
        if status_value == "rejected" or (mode == "live" and status_value != "approved"):
            continue
        entry = _profile_entry(root_id, root, path, metadata)
        prior = entries_by_runtime.get(entry["runtime_id"])
        if prior and canonical(prior) != canonical(entry):
            raise ValueError(f"conflicting runtime profile: {entry['runtime_id']}")
        entries_by_runtime[entry["runtime_id"]] = entry

    entries = [entries_by_runtime[key] for key in sorted(entries_by_runtime)]
    profile_set_sha256 = sha256(
        [
            {
                "runtime_id": entry["runtime_id"],
                "skill_tree_hash": entry["skill_tree_hash"],
                "metadata_sha256": entry["metadata_sha256"],
            }
            for entry in entries
        ]
    )
    base: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "mode": mode,
        "taxonomy_sha256": sha256(taxonomy),
        "router_sha256": router_sha256,
        "profile_set_sha256": profile_set_sha256,
        "previous_index_sha256": previous_index_sha256,
        "entries": entries,
    }
    return {**base, "content_sha256": sha256(base)}


def validate_index(index: Any, taxonomy: dict[str, Any]) -> list[str]:
    if not isinstance(index, dict):
        return ["index must be a JSON object"]
    errors: list[str] = []
    if index.get("schema_version") != INDEX_SCHEMA_VERSION:
        errors.append("index schema_version is invalid")
    if index.get("mode") not in {"shadow", "live"}:
        errors.append("index mode is invalid")
    claimed = index.get("content_sha256")
    base = {key: value for key, value in index.items() if key != "content_sha256"}
    if claimed != sha256(base):
        errors.append("index content_sha256 mismatch")
    if index.get("taxonomy_sha256") != sha256(taxonomy):
        errors.append("index taxonomy_sha256 mismatch")
    entries = index.get("entries")
    if not isinstance(entries, list):
        return errors + ["index entries must be an array"]
    seen: set[str] = set()
    for position, entry in enumerate(entries):
        prefix = f"entries[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        runtime_id = entry.get("runtime_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            errors.append(f"{prefix}.runtime_id is required")
        elif runtime_id in seen:
            errors.append(f"duplicate runtime_id: {runtime_id}")
        else:
            seen.add(runtime_id)
        metadata = entry.get("metadata")
        metadata_errors = validate_metadata(metadata, taxonomy)
        errors.extend(f"{prefix}: {error}" for error in metadata_errors)
        if isinstance(metadata, dict) and entry.get("metadata_sha256") != metadata_sha256(metadata):
            errors.append(f"{prefix}.metadata_sha256 mismatch")
        if isinstance(metadata, dict) and entry.get("declared_name") != metadata.get("skill"):
            errors.append(f"{prefix}.declared_name mismatch")
        root_id = entry.get("root_id")
        if not isinstance(root_id, str) or not ROOT_ID_RE.fullmatch(root_id):
            errors.append(f"{prefix}.root_id is invalid")
        relative_text = entry.get("relative_path")
        if not isinstance(relative_text, str) or not relative_text:
            errors.append(f"{prefix}.relative_path is required")
        else:
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(f"{prefix}.relative_path is unsafe")
        expected_suffix = (
            metadata.get("skill")
            if isinstance(metadata, dict) and relative_text == "."
            else relative_text
        )
        if isinstance(root_id, str) and isinstance(expected_suffix, str):
            if runtime_id != f"{root_id}:{expected_suffix}":
                errors.append(f"{prefix}.runtime_id mismatch")
        if not isinstance(entry.get("skill_tree_hash"), str) or not SHA256_RE.fullmatch(entry["skill_tree_hash"]):
            errors.append(f"{prefix}.skill_tree_hash is invalid")
        if entry.get("tree_hash_algorithm") != TREE_HASH_ALGORITHM:
            errors.append(f"{prefix}.tree_hash_algorithm mismatch")
    expected_profiles = sha256(
        [
            {
                "runtime_id": entry.get("runtime_id"),
                "skill_tree_hash": entry.get("skill_tree_hash"),
                "metadata_sha256": entry.get("metadata_sha256"),
            }
            for entry in entries
            if isinstance(entry, dict)
        ]
    )
    if index.get("profile_set_sha256") != expected_profiles:
        errors.append("index profile_set_sha256 mismatch")
    return errors


def validate_runtime_bindings(
    index: dict[str, Any],
    roots: Iterable[tuple[str, Path]],
) -> list[str]:
    root_map = {root_id: root.resolve() for root_id, root in roots}
    errors: list[str] = []
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        runtime_id = str(entry.get("runtime_id") or "unknown")
        root = root_map.get(str(entry.get("root_id") or ""))
        if root is None:
            errors.append(f"{runtime_id}: root binding unavailable")
            continue
        relative = Path(str(entry.get("relative_path") or "."))
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            errors.append(f"{runtime_id}: relative path escaped root")
            continue
        if not target.is_dir() or target.is_symlink():
            errors.append(f"{runtime_id}: skill root unavailable")
            continue
        if tree_hash(target) != entry.get("skill_tree_hash"):
            errors.append(f"{runtime_id}: skill tree hash drift")
    return errors


def _aliases(taxonomy: dict[str, Any], key: str, item_id: str) -> list[str]:
    for entry in taxonomy[key]:
        if entry["id"] == item_id:
            return [str(value) for value in entry.get("aliases", [])]
    return []


def _explicit_skill(prompt: str, skill: str) -> bool:
    escaped = re.escape(skill)
    return bool(
        re.search(rf"(?<![a-z0-9._-])\${escaped}(?![a-z0-9._-])", prompt)
        or re.search(rf"`{escaped}`", prompt)
        or re.search(rf"(?<![a-z0-9._-]){escaped}(?![a-z0-9._-])", prompt)
    )


def _hits(prompt: str, phrases: Iterable[str]) -> list[str]:
    return sorted({phrase for phrase in phrases if normalize(phrase) and normalize(phrase) in prompt})


def fallback_result(
    reason: str,
    *,
    errors: Iterable[str] = (),
    index_mode: str | None = None,
    excluded_runtime_ids: Iterable[str] = (),
    runtime_binding_verified: bool | None = False,
) -> dict[str, Any]:
    """Return a host handoff signal; this function never executes a Skill."""

    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "decision": "fallback",
        "recommended": [],
        "excluded_runtime_ids": sorted(excluded_runtime_ids),
        "fallback_used": True,
        "fallback_target": "legacy_catalog",
        "clarification_required": False,
        "safety_gate_required": False,
        "reason_codes": [reason],
        "index_errors": list(errors),
        "index_mode": index_mode,
        "advisory_only": True,
        "execution_performed": False,
        "authority_granted": False,
        "runtime_binding_verified": runtime_binding_verified,
    }


def route_prompt(
    prompt: str,
    index: dict[str, Any],
    taxonomy: dict[str, Any],
    *,
    top_k: int | None = None,
    runtime_roots: Iterable[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    index_errors = validate_index(index, taxonomy)
    if index_errors:
        return fallback_result(
            "index_invalid",
            errors=index_errors,
            index_mode=index.get("mode") if isinstance(index, dict) else None,
        )
    if index.get("mode") != "shadow":
        return fallback_result(
            "live_mode_not_enabled",
            index_mode=str(index.get("mode")),
        )
    binding_verified: bool | None = None
    if runtime_roots is not None:
        binding_errors = validate_runtime_bindings(index, runtime_roots)
        binding_verified = not binding_errors
        if binding_errors:
            return fallback_result(
                "runtime_binding_invalid",
                errors=binding_errors,
                index_mode="shadow",
            )
    normalized_prompt = normalize(prompt)
    ranked: list[dict[str, Any]] = []
    excluded: list[str] = []
    for entry in index["entries"]:
        metadata = entry["metadata"]
        explicit = _explicit_skill(normalized_prompt, metadata["skill"])
        negative_hits = _hits(normalized_prompt, metadata["negative_triggers"])
        trigger_hits = _hits(normalized_prompt, metadata["triggers"])
        domain_hits = _hits(
            normalized_prompt,
            _aliases(taxonomy, "domains", metadata["primary_domain"]),
        )
        capability_hits: list[str] = []
        for capability in metadata["capabilities"]:
            capability_hits.extend(
                _hits(normalized_prompt, _aliases(taxonomy, "capabilities", capability))
            )
        capability_hits = sorted(set(capability_hits))
        if negative_hits and not explicit:
            excluded.append(entry["runtime_id"])
            continue
        score = 100.0 if explicit else 0.0
        score += sum(12.0 + min(len(normalize(term)), 12) / 10.0 for term in trigger_hits)
        score += 3.0 * len(domain_hits)
        score += 2.0 * len(capability_hits)
        if score <= 0:
            continue
        reasons: list[str] = []
        if explicit:
            reasons.append("explicit_skill_name")
        if trigger_hits:
            reasons.append("positive_trigger")
        if domain_hits:
            reasons.append("domain_alias")
        if capability_hits:
            reasons.append("capability_alias")
        if negative_hits:
            reasons.append("negative_trigger_on_explicit_skill")
        ranked.append(
            {
                "runtime_id": entry["runtime_id"],
                "skill": metadata["skill"],
                "primary_domain": metadata["primary_domain"],
                "capabilities": metadata["capabilities"],
                "risk_tier": metadata["risk_tier"],
                "score": round(score, 3),
                "explicit_invocation": explicit,
                "negative_trigger_matched": bool(negative_hits),
                "reason_codes": reasons,
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["runtime_id"]))
    thresholds = taxonomy["thresholds"]
    limit = top_k or int(thresholds["default_top_k"])
    minimum_score = float(thresholds["minimum_score"])
    ranked = [item for item in ranked if item["score"] >= minimum_score][:limit]
    if not ranked:
        return fallback_result(
            "low_confidence_or_no_match",
            index_mode="shadow",
            excluded_runtime_ids=excluded,
            runtime_binding_verified=binding_verified,
        )

    top = ranked[0]
    decision = "recommend"
    clarification = False
    reason_codes: list[str] = []
    explicit_duplicates = [
        item
        for item in ranked
        if item["explicit_invocation"] and item["skill"] == top["skill"]
    ]
    if len(explicit_duplicates) > 1:
        decision = "clarify"
        clarification = True
        reason_codes.append("duplicate_explicit_skill_identity")
    elif top["explicit_invocation"] and top["negative_trigger_matched"]:
        decision = "clarify"
        clarification = True
        reason_codes.append("explicit_negative_conflict")
    elif not top["explicit_invocation"] and len(ranked) > 1:
        second = ranked[1]
        cross_domain = top["primary_domain"] != second["primary_domain"]
        if cross_domain and second["score"] >= top["score"] * float(thresholds["cross_domain_ratio"]):
            decision = "compose_or_clarify"
            clarification = True
            reason_codes.append("cross_domain_candidates")
        elif cross_domain and top["score"] - second["score"] < float(thresholds["ambiguous_margin"]):
            decision = "clarify"
            clarification = True
            reason_codes.append("ambiguous_margin")
    if decision == "compose_or_clarify":
        eligible = [
            item
            for item in ranked
            if item["score"] >= top["score"] * float(thresholds["cross_domain_ratio"])
        ]
        maximum = int(thresholds["max_composition"])
        if len(eligible) > maximum:
            decision = "clarify"
            reason_codes.append("composition_limit_exceeded")
        ranked = ranked[:maximum]
    safety_gate = any(item["risk_tier"] == "high" for item in ranked[:3])
    if safety_gate:
        reason_codes.append("high_risk_requires_authority_check")
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "decision": decision,
        "recommended": ranked,
        "excluded_runtime_ids": sorted(excluded),
        "fallback_used": False,
        "fallback_target": "legacy_catalog",
        "clarification_required": clarification,
        "safety_gate_required": safety_gate,
        "reason_codes": reason_codes,
        "index_errors": [],
        "index_mode": "shadow",
        "advisory_only": True,
        "execution_performed": False,
        "authority_granted": False,
        "runtime_binding_verified": binding_verified,
    }


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, raw = line.split(":", 1)
        values[key.strip()] = raw.strip().strip("'\"")
    return values


def suggest_metadata(skill_root: Path, taxonomy: dict[str, Any] | None = None) -> dict[str, Any]:
    taxonomy = taxonomy or load_taxonomy()
    frontmatter = _frontmatter(skill_root / "SKILL.md")
    skill = frontmatter.get("name") or skill_root.name
    if not SKILL_RE.fullmatch(skill):
        raise ValueError("SKILL.md frontmatter name is missing or invalid")
    corpus = normalize(f"{skill} {frontmatter.get('description', '')}")
    domain_scores: list[tuple[int, str]] = []
    for entry in taxonomy["domains"]:
        score = sum(1 for alias in entry.get("aliases", []) if normalize(alias) in corpus)
        domain_scores.append((score, entry["id"]))
    domain_scores.sort(key=lambda item: (-item[0], item[1]))
    primary_domain = domain_scores[0][1] if domain_scores and domain_scores[0][0] else "uncategorized"
    capabilities = sorted(
        entry["id"]
        for entry in taxonomy["capabilities"]
        if any(normalize(alias) in corpus for alias in entry.get("aliases", []))
    )
    if not capabilities:
        capabilities = ["general-assistance"]
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "skill": skill,
        "routing_metadata_version": 1,
        "primary_domain": primary_domain,
        "capabilities": capabilities,
        "triggers": [skill],
        "negative_triggers": [],
        "input_types": ["user_prompt"],
        "output_types": ["task_result"],
        "dependencies": [],
        "risk_tier": "medium",
        "allowed_actions": ["advisory_selection"],
        "prohibited_actions": ["automatic_execution", "automatic_promotion"],
        "clarify_when": ["分类置信度低或与其他领域候选接近"],
        "fallback_mode": "legacy_catalog",
        "classification": {
            "method": "deterministic_suggestion",
            "status": "suggested",
            "evidence": ["frontmatter-only deterministic suggestion; human review required"],
        },
    }


def catalog_baseline(roots: Iterable[tuple[str, Path]], host_loading_mode: str) -> dict[str, Any]:
    totals = {"skills": 0, "skill_body_bytes": 0, "description_characters": 0, "description_bytes": 0}
    per_root: list[dict[str, Any]] = []
    for root_id, root in roots:
        root_totals = {"root_id": root_id, "skills": 0, "skill_body_bytes": 0, "description_characters": 0, "description_bytes": 0}
        for path in sorted(root.rglob("SKILL.md")):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts) or path.is_symlink():
                continue
            raw = path.read_bytes()
            description = _frontmatter(path).get("description", "")
            root_totals["skills"] += 1
            root_totals["skill_body_bytes"] += len(raw)
            root_totals["description_characters"] += len(description)
            root_totals["description_bytes"] += len(description.encode("utf-8"))
        per_root.append(root_totals)
        for key in totals:
            totals[key] += int(root_totals[key])
    characters = totals["description_characters"]
    return {
        "schema_version": "skill-sublation.catalog-baseline.v1",
        "host_loading_mode": host_loading_mode,
        "token_savings_claim_allowed": host_loading_mode == "deferred",
        "measurement_scope": "frontmatter descriptions and SKILL.md byte counts only",
        "totals": totals,
        "description_token_proxy_range": {
            "lower_bound": math.ceil(characters / 4),
            "upper_bound": characters,
            "note": "character proxy only; use a host tokenizer for billing or model-specific claims",
        },
        "roots": per_root,
    }


def _load_attestation_key(path: Path) -> bytes:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("attestation key must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("attestation key must not be group/world accessible")
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("attestation key must contain at least 32 bytes")
    return key


def _load_external_attestation_key(path: Path) -> bytes:
    """Load a 0600 key that is stored outside this candidate tree."""

    key = _load_attestation_key(path)
    candidate_root = Path(__file__).resolve().parent.parent
    try:
        path.resolve(strict=True).relative_to(candidate_root)
    except ValueError:
        return key
    raise ValueError("authorized shadow attestation key must be external to the candidate tree")


def prompt_fingerprint(prompt: str, key_path: Path) -> str:
    """Return a keyed prompt fingerprint without persisting prompt content."""

    key = _load_attestation_key(key_path)
    return "sha256:" + hmac.new(
        key, prompt.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _previous_event_hash(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    previous = json.loads(lines[-1])
    value = previous.get("event_sha256")
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("observation log tail lacks event_sha256")
    return value


def observation_event_errors(event: Any, *, signed: bool) -> list[str]:
    """Validate the strict privacy-preserving observation event contract."""

    if not isinstance(event, dict):
        return ["observation event must be an object"]
    required = set(OBSERVATION_BASE_KEYS)
    if signed:
        required.update({"previous_event_sha256", "event_sha256", "hmac_sha256"})
    errors: list[str] = []
    unknown = sorted(set(event) - required)
    missing = sorted(required - set(event))
    if unknown:
        errors.append("observation event has unsupported keys: " + ", ".join(unknown))
    if missing:
        errors.append("observation event is missing keys: " + ", ".join(missing))
    if event.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        errors.append("observation schema_version mismatch")
    if event.get("mode") != "shadow":
        errors.append("observation mode must be shadow")
    if not isinstance(event.get("request_id"), str) or not OPAQUE_ID_RE.fullmatch(event["request_id"]):
        errors.append("observation request_id must be an opaque identifier")
    if event.get("evaluation_cohort") not in FIXTURE_CASE_TYPES:
        errors.append("observation evaluation_cohort is invalid")
    if not isinstance(event.get("prompt_fingerprint"), str) or not SHA256_RE.fullmatch(
        event["prompt_fingerprint"]
    ):
        errors.append("observation prompt_fingerprint must be a sha256 digest")
    if not isinstance(event.get("deterministic_replay_verified"), bool):
        errors.append("observation deterministic_replay_verified must be boolean")
    for key in ("index_sha256", "router_sha256", "profile_set_sha256"):
        if not isinstance(event.get(key), str) or not SHA256_RE.fullmatch(event[key]):
            errors.append(f"observation {key} must be a sha256 digest")
    if event.get("decision") not in {"recommend", "clarify", "compose_or_clarify", "fallback"}:
        errors.append("observation decision is invalid")
    for key in ("recommended_runtime_ids", "reason_codes"):
        value = event.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and 0 < len(item) <= 256 and "\n" not in item
            for item in value
        ):
            errors.append(f"observation {key} must be a bounded string array")
    for key in ("fallback_used", "clarification_required", "safety_gate_required"):
        if not isinstance(event.get(key), bool):
            errors.append(f"observation {key} must be boolean")
    if event.get("authority_granted") is not False:
        errors.append("observation authority_granted must be false")
    if event.get("raw_prompt_stored") is not False:
        errors.append("observation raw_prompt_stored must be false")
    runtime_binding = event.get("runtime_binding_verified")
    if runtime_binding is not True and runtime_binding is not False and runtime_binding is not None:
        errors.append("observation runtime_binding_verified is invalid")
    baseline = event.get("baseline_skill")
    if baseline is not None and (
        not isinstance(baseline, str)
        or not SKILL_RE.fullmatch(baseline)
    ):
        errors.append("observation baseline_skill must be a canonical Skill id or null")
    latency = event.get("latency_ms")
    try:
        latency_number = float(latency)
    except (TypeError, ValueError, OverflowError):
        latency_number = math.nan
    if not isinstance(latency, (int, float)) or isinstance(latency, bool) or not math.isfinite(latency_number) or latency_number < 0:
        errors.append("observation latency_ms must be a nonnegative finite number")
    timestamp = event.get("ts")
    if not isinstance(timestamp, str) or not timestamp:
        errors.append("observation ts is required")
    else:
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            errors.append("observation ts must be an ISO-8601 timestamp")
        else:
            if parsed_timestamp.tzinfo is None:
                errors.append("observation ts must include a timezone")
    if signed:
        previous = event.get("previous_event_sha256")
        if previous is not None and (
            not isinstance(previous, str) or not SHA256_RE.fullmatch(previous)
        ):
            errors.append("observation previous_event_sha256 is invalid")
        for key in ("event_sha256", "hmac_sha256"):
            if not isinstance(event.get(key), str) or not SHA256_RE.fullmatch(event[key]):
                errors.append(f"observation {key} is invalid")
    return errors


def append_observation(
    log_path: Path,
    key_path: Path,
    event: dict[str, Any],
) -> dict[str, Any]:
    contract_errors = observation_event_errors(event, signed=False)
    if contract_errors:
        raise ValueError("; ".join(contract_errors))
    key = _load_attestation_key(key_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and (log_path.is_symlink() or not log_path.is_file()):
        raise ValueError("observation log must be a regular non-symlink file")
    base = {**event, "previous_event_sha256": _previous_event_hash(log_path)}
    event_sha256 = sha256(base)
    signed = {
        **base,
        "event_sha256": event_sha256,
        "hmac_sha256": "sha256:" + hmac.new(key, event_sha256.encode("ascii"), hashlib.sha256).hexdigest(),
    }
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(log_path, 0o600)
    return signed


def verify_observation_log(
    log_path: Path,
    key_path: Path,
    *,
    key_id: str = "unspecified",
) -> dict[str, Any]:
    """Verify a HMAC-chained redacted journal and return a shareable receipt."""

    key = _load_attestation_key(key_path)
    if not OPAQUE_ID_RE.fullmatch(key_id):
        raise ValueError("attestation key id must be opaque")
    metadata = os.lstat(log_path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("observation log must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("observation log must not be group/world accessible")
    raw = log_path.read_bytes()
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError("observation log is empty")
    previous_hash: str | None = None
    first_hash: str | None = None
    request_ids: set[str] = set()
    artifact_hashes: dict[str, set[str]] = {
        "index_sha256": set(),
        "router_sha256": set(),
        "profile_set_sha256": set(),
    }
    runtime_bindings: list[bool | None] = []
    deterministic_replays: list[bool] = []
    latencies: list[float] = []
    result_projections: list[dict[str, Any]] = []
    plan_projections: list[dict[str, Any]] = []
    first_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    for position, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"observation line {position} is not valid JSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"observation line {position} must be an object")
        contract_errors = observation_event_errors(event, signed=True)
        if contract_errors:
            raise ValueError(
                f"observation line {position} violates the event contract: "
                + "; ".join(contract_errors)
            )
        event_hash = event.get("event_sha256")
        signature = event.get("hmac_sha256")
        if not isinstance(event_hash, str) or not SHA256_RE.fullmatch(event_hash):
            raise ValueError(f"observation line {position} has an invalid event hash")
        if not isinstance(signature, str) or not SHA256_RE.fullmatch(signature):
            raise ValueError(f"observation line {position} has an invalid HMAC")
        base = {
            key_name: value
            for key_name, value in event.items()
            if key_name not in {"event_sha256", "hmac_sha256"}
        }
        if base.get("previous_event_sha256") != previous_hash:
            raise ValueError(f"observation line {position} breaks the hash chain")
        if sha256(base) != event_hash:
            raise ValueError(f"observation line {position} content hash mismatch")
        expected_signature = "sha256:" + hmac.new(
            key, event_hash.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError(f"observation line {position} HMAC mismatch")
        if first_hash is None:
            first_hash = event_hash
        request_id = str(event["request_id"])
        if request_id in request_ids:
            raise ValueError(f"observation line {position} reuses request_id")
        request_ids.add(request_id)
        for key_name in artifact_hashes:
            artifact_hashes[key_name].add(str(event[key_name]))
        runtime_bindings.append(event.get("runtime_binding_verified"))
        deterministic_replays.append(event["deterministic_replay_verified"])
        result_projections.append(
            {
                "case_id": request_id,
                "case_type": event["evaluation_cohort"],
                "decision": event["decision"],
                "recommended_runtime_ids": event["recommended_runtime_ids"],
                "fallback_used": event["fallback_used"],
                "clarification_required": event["clarification_required"],
                "safety_gate_required": event["safety_gate_required"],
                "reason_codes": event["reason_codes"],
                "runtime_binding_verified": event["runtime_binding_verified"],
                "deterministic_replay_verified": event[
                    "deterministic_replay_verified"
                ],
            }
        )
        plan_projections.append(
            {
                "case_id": request_id,
                "case_type": event["evaluation_cohort"],
                "prompt_fingerprint": event["prompt_fingerprint"],
            }
        )
        latencies.append(float(event["latency_ms"]))
        parsed_timestamp = datetime.fromisoformat(
            str(event["ts"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if previous_timestamp is not None and parsed_timestamp < previous_timestamp:
            raise ValueError(f"observation line {position} timestamp is not monotonic")
        if first_timestamp is None:
            first_timestamp = parsed_timestamp
        previous_timestamp = parsed_timestamp
        last_timestamp = parsed_timestamp
        previous_hash = event_hash
    for key_name, values in artifact_hashes.items():
        if len(values) != 1:
            raise ValueError(f"observation log contains multiple {key_name} values")
    sorted_latencies = sorted(latencies)
    p95_position = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
    return {
        "schema_version": OBSERVATION_VERIFICATION_SCHEMA_VERSION,
        "status": "passed",
        "log_sha256": sha256(raw),
        "event_count": len(lines),
        "first_event_sha256": first_hash,
        "last_event_sha256": previous_hash,
        "chain_verified": True,
        "hmac_verified": True,
        "privacy_flags_verified": True,
        "unique_request_ids_verified": True,
        "runtime_binding_true_count": sum(value is True for value in runtime_bindings),
        "runtime_binding_false_count": sum(value is False for value in runtime_bindings),
        "runtime_binding_null_count": sum(value is None for value in runtime_bindings),
        "deterministic_replay_true_count": sum(deterministic_replays),
        "deterministic_replay_false_count": sum(
            not value for value in deterministic_replays
        ),
        "index_sha256": next(iter(artifact_hashes["index_sha256"])),
        "router_sha256": next(iter(artifact_hashes["router_sha256"])),
        "profile_set_sha256": next(iter(artifact_hashes["profile_set_sha256"])),
        "attestation_key_id": key_id,
        "attestation_key_sha256": sha256(key),
        "request_id_set_sha256": sha256(sorted(request_ids)),
        "result_projection_sha256": sha256(
            sorted(result_projections, key=lambda item: item["case_id"])
        ),
        "plan_projection_sha256": sha256(
            sorted(plan_projections, key=lambda item: item["case_id"])
        ),
        "p95_latency_ms": round(sorted_latencies[p95_position], 3),
        "first_ts": first_timestamp.isoformat() if first_timestamp else None,
        "last_ts": last_timestamp.isoformat() if last_timestamp else None,
    }


def shadow_event(
    route: dict[str, Any],
    index: dict[str, Any],
    *,
    request_id: str,
    baseline_skill: str | None,
    latency_ms: float,
    evaluation_cohort: str = "general",
    prompt_fingerprint: str,
    deterministic_replay_verified: bool,
) -> dict[str, Any]:
    if index.get("mode") != "shadow" or route.get("index_mode") != "shadow":
        raise ValueError("shadow observation requires a shadow index and shadow route result")
    if route.get("advisory_only") is not True or route.get("authority_granted") is not False:
        raise ValueError("shadow observation requires a non-authoritative route result")
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "evaluation_cohort": evaluation_cohort,
        "prompt_fingerprint": prompt_fingerprint,
        "deterministic_replay_verified": deterministic_replay_verified,
        "mode": "shadow",
        "index_sha256": index.get("content_sha256", "sha256:" + "0" * 64),
        "router_sha256": index.get("router_sha256", "sha256:" + "0" * 64),
        "profile_set_sha256": index.get("profile_set_sha256", "sha256:" + "0" * 64),
        "decision": route["decision"],
        "recommended_runtime_ids": [item["runtime_id"] for item in route.get("recommended", [])],
        "fallback_used": bool(route["fallback_used"]),
        "clarification_required": bool(route["clarification_required"]),
        "safety_gate_required": bool(route["safety_gate_required"]),
        "reason_codes": list(route.get("reason_codes", [])),
        "latency_ms": round(latency_ms, 3),
        "baseline_skill": baseline_skill,
        "authority_granted": False,
        "raw_prompt_stored": False,
        "runtime_binding_verified": route.get("runtime_binding_verified"),
    }


def validate_fixture_report(report: Any) -> list[str]:
    """Validate a report whose string fields cannot carry task or case text."""

    if not isinstance(report, dict):
        return ["routing fixture report must be an object"]
    required = {
        "schema_version",
        "fixture_id",
        "sample_source",
        "authorization_evidence",
        "index_sha256",
        "case_id_set_sha256",
        "fixture_plan_sha256",
        "result_projection_sha256",
        "total",
        "passed",
        "failed",
        "pass_rate",
        "raw_prompt_leaks",
        "metrics",
        "results",
    }
    errors: list[str] = []
    unknown = sorted(set(report) - required)
    missing = sorted(required - set(report))
    if unknown:
        errors.append("routing fixture report has unsupported keys: " + ", ".join(unknown))
    if missing:
        errors.append("routing fixture report is missing keys: " + ", ".join(missing))
    if report.get("schema_version") != "skill-sublation.routing-fixture-report.v1":
        errors.append("routing fixture report schema_version mismatch")
    if not isinstance(report.get("fixture_id"), str) or not OPAQUE_ID_RE.fullmatch(report["fixture_id"]):
        errors.append("routing fixture report fixture_id must be opaque")
    if report.get("sample_source") not in {"synthetic_fixture", "authorized_shadow_runtime"}:
        errors.append("routing fixture report sample_source is invalid")
    evidence = report.get("authorization_evidence")
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) and OPAQUE_ID_RE.fullmatch(item) for item in evidence
    ):
        errors.append("routing fixture report authorization_evidence must contain opaque ids")
    if not isinstance(report.get("index_sha256"), str) or not SHA256_RE.fullmatch(report["index_sha256"]):
        errors.append("routing fixture report index_sha256 is invalid")
    if not isinstance(report.get("case_id_set_sha256"), str) or not SHA256_RE.fullmatch(report["case_id_set_sha256"]):
        errors.append("routing fixture report case_id_set_sha256 is invalid")
    if not isinstance(report.get("fixture_plan_sha256"), str) or not SHA256_RE.fullmatch(report["fixture_plan_sha256"]):
        errors.append("routing fixture report fixture_plan_sha256 is invalid")
    if not isinstance(report.get("result_projection_sha256"), str) or not SHA256_RE.fullmatch(report["result_projection_sha256"]):
        errors.append("routing fixture report result_projection_sha256 is invalid")
    for key in ("total", "passed", "failed", "raw_prompt_leaks"):
        if not isinstance(report.get(key), int) or isinstance(report.get(key), bool) or report[key] < 0:
            errors.append(f"routing fixture report {key} must be a nonnegative integer")
    pass_rate = report.get("pass_rate")
    try:
        pass_rate_number = float(pass_rate)
    except (TypeError, ValueError, OverflowError):
        pass_rate_number = math.nan
    if not isinstance(pass_rate, (int, float)) or isinstance(pass_rate, bool) or not math.isfinite(pass_rate_number) or not 0 <= pass_rate_number <= 1:
        errors.append("routing fixture report pass_rate must be between 0 and 1")
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("routing fixture report metrics must be an object")
    else:
        if set(metrics) != ROUTING_METRIC_KEYS:
            errors.append("routing fixture report metrics keys do not match the v1 contract")
    results = report.get("results")
    result_keys = {
        "case_id",
        "case_type",
        "passed",
        "failure_codes",
        "decision",
        "recommended_runtime_ids",
        "safety_gate_required",
        "fallback_used",
        "clarification_required",
        "reason_codes",
        "runtime_binding_verified",
        "deterministic_replay_verified",
    }
    if not isinstance(results, list):
        errors.append("routing fixture report results must be an array")
    else:
        seen_ids: set[str] = set()
        for position, result in enumerate(results):
            prefix = f"routing fixture report results[{position}]"
            if not isinstance(result, dict) or set(result) != result_keys:
                errors.append(f"{prefix} keys do not match the privacy contract")
                continue
            case_id = result.get("case_id")
            if not isinstance(case_id, str) or not OPAQUE_ID_RE.fullmatch(case_id):
                errors.append(f"{prefix}.case_id must be opaque")
            elif case_id in seen_ids:
                errors.append(f"{prefix}.case_id is duplicated")
            else:
                seen_ids.add(case_id)
            if result.get("case_type") not in FIXTURE_CASE_TYPES:
                errors.append(f"{prefix}.case_type is invalid")
            if not isinstance(result.get("passed"), bool):
                errors.append(f"{prefix}.passed must be boolean")
            failures = result.get("failure_codes")
            if not isinstance(failures, list) or not all(
                isinstance(item, str) and item in FIXTURE_FAILURE_CODES
                for item in failures
            ):
                errors.append(f"{prefix}.failure_codes is invalid")
            elif len(failures) != len(set(failures)):
                errors.append(f"{prefix}.failure_codes is invalid")
            if result.get("decision") not in {
                "recommend",
                "clarify",
                "compose_or_clarify",
                "fallback",
            }:
                errors.append(f"{prefix}.decision is invalid")
            if not isinstance(result.get("safety_gate_required"), bool):
                errors.append(f"{prefix}.safety_gate_required must be boolean")
            for key in ("fallback_used", "clarification_required"):
                if not isinstance(result.get(key), bool):
                    errors.append(f"{prefix}.{key} must be boolean")
            reason_codes = result.get("reason_codes")
            if not isinstance(reason_codes, list) or not all(
                isinstance(item, str) and OPAQUE_ID_RE.fullmatch(item)
                for item in reason_codes
            ):
                errors.append(f"{prefix}.reason_codes is invalid")
            runtime_binding = result.get("runtime_binding_verified")
            if runtime_binding is not True and runtime_binding is not False and runtime_binding is not None:
                errors.append(f"{prefix}.runtime_binding_verified is invalid")
            if not isinstance(result.get("deterministic_replay_verified"), bool):
                errors.append(f"{prefix}.deterministic_replay_verified must be boolean")
            recommended = result.get("recommended_runtime_ids")
            if not isinstance(recommended, list) or not all(
                isinstance(item, str)
                and 0 < len(item) <= 256
                and "\n" not in item
                and "\r" not in item
                for item in recommended
            ):
                errors.append(f"{prefix}.recommended_runtime_ids is invalid")
    return errors


def redacted_fixture_plan(
    index: dict[str, Any],
    fixture: dict[str, Any],
    *,
    fingerprint_key: Path | None = None,
) -> dict[str, Any]:
    """Build the strict prompt-free plan that authorization and events bind."""

    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture.cases must be a nonempty array")
    sample_source = fixture.get("sample_source", "synthetic_fixture")
    if sample_source == "authorized_shadow_runtime" and fingerprint_key is None:
        raise ValueError("authorized shadow fixture plans require an attestation key")
    if sample_source == "authorized_shadow_runtime" and fingerprint_key is not None:
        _load_external_attestation_key(fingerprint_key)
    fingerprint_type = (
        "hmac-sha256" if fingerprint_key is not None else "sha256-synthetic-only"
    )
    redacted_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("fixture plan cases must be objects")
        case_id = case.get("id")
        prompt = case.get("prompt")
        case_type = case.get("case_type", "general")
        if not isinstance(case_id, str) or not OPAQUE_ID_RE.fullmatch(case_id):
            raise ValueError("fixture plan case ids must be opaque")
        if case_id in seen_ids:
            raise ValueError(f"fixture plan case id is duplicated: {case_id}")
        seen_ids.add(case_id)
        if not isinstance(prompt, str) or not prompt:
            raise ValueError(f"fixture plan case {case_id} requires a prompt")
        if case_type not in FIXTURE_CASE_TYPES:
            raise ValueError(f"fixture plan case {case_id} has an invalid case_type")
        expected_contains = case.get("expected_contains", [])
        expected_excludes = case.get("expected_excludes", [])
        if not _string_list(expected_contains) or not _string_list(expected_excludes):
            raise ValueError(f"fixture plan case {case_id} expectations must be string arrays")
        fingerprint = (
            prompt_fingerprint(prompt, fingerprint_key)
            if fingerprint_key is not None
            else sha256(prompt.encode("utf-8"))
        )
        redacted_cases.append(
            {
                "case_id": case_id,
                "prompt_fingerprint": fingerprint,
                "case_type": case_type,
                "expected_decision": case.get("expected_decision"),
                "expected_top": case.get("expected_top"),
                "expected_contains": expected_contains,
                "expected_excludes": expected_excludes,
                "expected_safety_gate": case.get("expected_safety_gate"),
            }
        )
    base = {
        "schema_version": "skill-sublation.routing-fixture-plan.v1",
        "fixture_id": fixture.get("fixture_id", "unnamed"),
        "sample_source": sample_source,
        "index_sha256": index.get("content_sha256"),
        "prompt_fingerprint_type": fingerprint_type,
        "cases": sorted(redacted_cases, key=lambda item: item["case_id"]),
    }
    plan = {**base, "content_sha256": sha256(base)}
    known_runtime_ids = {
        entry.get("runtime_id")
        for entry in index.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("runtime_id"), str)
    }
    errors = validate_fixture_plan(plan, known_runtime_ids=known_runtime_ids)
    if errors:
        raise ValueError("; ".join(errors))
    return plan


def validate_fixture_plan(
    plan: Any,
    *,
    known_runtime_ids: set[str],
) -> list[str]:
    if not isinstance(plan, dict):
        return ["routing fixture plan must be an object"]
    required = {
        "schema_version",
        "fixture_id",
        "sample_source",
        "index_sha256",
        "prompt_fingerprint_type",
        "cases",
        "content_sha256",
    }
    errors: list[str] = []
    if set(plan) != required:
        errors.append("routing fixture plan keys do not match the privacy contract")
    if plan.get("schema_version") != "skill-sublation.routing-fixture-plan.v1":
        errors.append("routing fixture plan schema_version mismatch")
    if not isinstance(plan.get("fixture_id"), str) or not OPAQUE_ID_RE.fullmatch(
        plan["fixture_id"]
    ):
        errors.append("routing fixture plan fixture_id must be opaque")
    if plan.get("sample_source") not in {
        "synthetic_fixture",
        "authorized_shadow_runtime",
    }:
        errors.append("routing fixture plan sample_source is invalid")
    if plan.get("prompt_fingerprint_type") not in {
        "sha256-synthetic-only",
        "hmac-sha256",
    }:
        errors.append("routing fixture plan prompt_fingerprint_type is invalid")
    if plan.get("sample_source") == "authorized_shadow_runtime" and plan.get(
        "prompt_fingerprint_type"
    ) != "hmac-sha256":
        errors.append("authorized routing fixture plans require keyed prompt fingerprints")
    for key in ("index_sha256", "content_sha256"):
        if not isinstance(plan.get(key), str) or not SHA256_RE.fullmatch(plan[key]):
            errors.append(f"routing fixture plan {key} is invalid")
    cases = plan.get("cases")
    case_keys = {
        "case_id",
        "prompt_fingerprint",
        "case_type",
        "expected_decision",
        "expected_top",
        "expected_contains",
        "expected_excludes",
        "expected_safety_gate",
    }
    seen_ids: set[str] = set()
    if not isinstance(cases, list) or not cases:
        errors.append("routing fixture plan cases must be a nonempty array")
    else:
        for position, case in enumerate(cases):
            prefix = f"routing fixture plan cases[{position}]"
            if not isinstance(case, dict) or set(case) != case_keys:
                errors.append(f"{prefix} keys are invalid")
                continue
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or not OPAQUE_ID_RE.fullmatch(case_id):
                errors.append(f"{prefix}.case_id is invalid")
            elif case_id in seen_ids:
                errors.append(f"{prefix}.case_id is duplicated")
            else:
                seen_ids.add(case_id)
            if not isinstance(case.get("prompt_fingerprint"), str) or not SHA256_RE.fullmatch(
                case["prompt_fingerprint"]
            ):
                errors.append(f"{prefix}.prompt_fingerprint is invalid")
            if case.get("case_type") not in FIXTURE_CASE_TYPES:
                errors.append(f"{prefix}.case_type is invalid")
            if case.get("expected_decision") not in {
                None,
                "recommend",
                "clarify",
                "compose_or_clarify",
                "fallback",
            }:
                errors.append(f"{prefix}.expected_decision is invalid")
            expected_top = case.get("expected_top")
            if expected_top is not None and (
                not isinstance(expected_top, str)
                or not 0 < len(expected_top) <= 512
                or ":" not in expected_top
                or any(character in expected_top for character in "\r\n\t")
            ):
                errors.append(f"{prefix}.expected_top is invalid")
            elif expected_top is not None and expected_top not in known_runtime_ids:
                errors.append(f"{prefix}.expected_top is outside the bound index")
            for key in ("expected_contains", "expected_excludes"):
                values = case.get(key)
                if (
                    not _string_list(values)
                    or len(values) != len(set(values))
                    or any(
                        len(item) > 512
                        or ":" not in item
                        or any(character in item for character in "\r\n\t")
                        for item in values
                    )
                ):
                    errors.append(f"{prefix}.{key} is invalid")
                elif any(item not in known_runtime_ids for item in values):
                    errors.append(f"{prefix}.{key} contains an identity outside the bound index")
            if case.get("expected_safety_gate") not in {None, True, False}:
                errors.append(f"{prefix}.expected_safety_gate is invalid")
            case_type = case.get("case_type")
            if case_type in {"single_domain", "explicit", "high_risk"} and expected_top is None:
                errors.append(f"{prefix}.expected_top is required for this cohort")
            if case_type == "multi_domain" and len(case.get("expected_contains", [])) < 2:
                errors.append(f"{prefix}.expected_contains requires two runtime ids")
            if case_type == "negative_control" and (
                expected_top is None or not case.get("expected_excludes")
            ):
                errors.append(f"{prefix} negative control expectations are incomplete")
            if case_type in {"fallback", "tree_drift"} and case.get("expected_decision") != "fallback":
                errors.append(f"{prefix}.expected_decision must be fallback")
            if case_type == "high_risk" and case.get("expected_safety_gate") is not True:
                errors.append(f"{prefix}.expected_safety_gate must be true")
    if set(plan) == required:
        base = {key: value for key, value in plan.items() if key != "content_sha256"}
        if plan.get("content_sha256") != sha256(base):
            errors.append("routing fixture plan content_sha256 mismatch")
    return errors


def verify_supplied_fixture_plan(
    index: dict[str, Any],
    fixture: dict[str, Any],
    supplied_plan: Any,
    *,
    fingerprint_key: Path | None = None,
) -> dict[str, Any]:
    """Bind a pre-existing redacted plan to the exact index, fixture and key."""

    known_runtime_ids = {
        entry.get("runtime_id")
        for entry in index.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("runtime_id"), str)
    }
    errors = validate_fixture_plan(
        supplied_plan,
        known_runtime_ids=known_runtime_ids,
    )
    if errors:
        raise ValueError("supplied fixture plan is invalid: " + "; ".join(errors))
    expected = redacted_fixture_plan(
        index,
        fixture,
        fingerprint_key=fingerprint_key,
    )
    supplied_canonical = canonical(supplied_plan)
    expected_canonical = canonical(expected)
    if (
        supplied_canonical != expected_canonical
        or sha256(supplied_canonical) != sha256(expected_canonical)
    ):
        raise ValueError(
            "supplied fixture plan does not match the exact index, fixture and attestation key"
        )
    return supplied_plan


def evaluate_fixture(
    index: dict[str, Any],
    taxonomy: dict[str, Any],
    fixture: dict[str, Any],
    *,
    runtime_roots: Iterable[tuple[str, Path]] | None = None,
    fingerprint_key: Path | None = None,
    observation_log: Path | None = None,
    baseline_skill: str | None = None,
    supplied_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases = fixture.get("cases")
    normalized_runtime_roots = list(runtime_roots) if runtime_roots is not None else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture.cases must be a nonempty array")
    sample_source = fixture.get("sample_source", "synthetic_fixture")
    if sample_source not in {"synthetic_fixture", "authorized_shadow_runtime"}:
        raise ValueError("fixture.sample_source is invalid")
    authorization_evidence = fixture.get("authorization_evidence", [])
    if sample_source == "authorized_shadow_runtime" and not _string_list(
        authorization_evidence, nonempty=True
    ):
        raise ValueError("authorized shadow runtime fixtures require authorization_evidence")
    if (
        sample_source == "authorized_shadow_runtime"
        and observation_log is not None
        and supplied_plan is None
    ):
        raise ValueError(
            "authorized shadow observation requires a pre-existing supplied fixture plan"
        )
    plan = (
        verify_supplied_fixture_plan(
            index,
            fixture,
            supplied_plan,
            fingerprint_key=fingerprint_key,
        )
        if supplied_plan is not None
        else redacted_fixture_plan(index, fixture, fingerprint_key=fingerprint_key)
    )
    if observation_log is not None and fingerprint_key is None:
        raise ValueError("fixture observation logging requires an attestation key")
    if observation_log is not None and observation_log.exists() and observation_log.stat().st_size:
        raise ValueError("fixture observation log must be new or empty")
    plan_by_id = {case["case_id"]: case for case in plan["cases"]}
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    deterministic_matches = 0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("prompt"), str):
            raise ValueError("each fixture case requires id and prompt")
        case_type = case.get("case_type", "general")
        if case_type not in FIXTURE_CASE_TYPES:
            raise ValueError(f"fixture case {case['id']} has an invalid case_type")
        if case_type in {"single_domain", "explicit", "high_risk"} and not isinstance(case.get("expected_top"), str):
            raise ValueError(f"fixture case {case['id']} requires expected_top")
        if case_type == "explicit" and "$" not in case["prompt"]:
            raise ValueError(f"fixture explicit case {case['id']} must use explicit $skill syntax")
        if case_type == "multi_domain" and (
            not isinstance(case.get("expected_contains"), list)
            or len(case["expected_contains"]) < 2
        ):
            raise ValueError(f"fixture multi_domain case {case['id']} requires two expected skills")
        if case_type == "negative_control" and (
            not isinstance(case.get("expected_excludes"), list)
            or not case["expected_excludes"]
            or not isinstance(case.get("expected_top"), str)
        ):
            raise ValueError(f"fixture negative_control case {case['id']} requires expected_top and expected_excludes")
        if case_type in {"fallback", "tree_drift"} and case.get("expected_decision") != "fallback":
            raise ValueError(f"fixture case {case['id']} must expect fallback")
        if case_type == "high_risk" and case.get("expected_safety_gate") is not True:
            raise ValueError(f"fixture high_risk case {case['id']} must require the safety gate")
        if case_type == "tree_drift" and normalized_runtime_roots is None:
            raise ValueError(f"fixture tree_drift case {case['id']} requires runtime roots")
        case_runtime_roots = normalized_runtime_roots
        drift_directory: tempfile.TemporaryDirectory[str] | None = None
        if case_type == "tree_drift":
            if not index.get("entries"):
                raise ValueError("tree_drift fixture requires a nonempty index")
            drift_directory = tempfile.TemporaryDirectory(
                prefix="skill-router-tree-drift-"
            )
            drift_root = Path(drift_directory.name)
            copied_roots: list[tuple[str, Path]] = []
            for root_id, source_root in normalized_runtime_roots or []:
                copied = drift_root / root_id
                shutil.copytree(source_root, copied, symlinks=True)
                copied_roots.append((root_id, copied))
            first_entry = index["entries"][0]
            selected_root = next(
                (
                    root
                    for root_id, root in copied_roots
                    if root_id == first_entry.get("root_id")
                ),
                None,
            )
            if selected_root is None:
                drift_directory.cleanup()
                raise ValueError("tree_drift fixture cannot locate the indexed runtime root")
            drifted_skill = selected_root / str(first_entry.get("relative_path")) / "SKILL.md"
            if not drifted_skill.is_file() or drifted_skill.is_symlink():
                drift_directory.cleanup()
                raise ValueError("tree_drift fixture cannot locate a regular SKILL.md")
            with drifted_skill.open("a", encoding="utf-8") as handle:
                handle.write("\n# isolated tree-drift fixture mutation\n")
            case_runtime_roots = copied_roots
        try:
            started = time.perf_counter()
            route = route_prompt(
                case["prompt"],
                index,
                taxonomy,
                runtime_roots=case_runtime_roots,
            )
            measured_latency = (time.perf_counter() - started) * 1000
            latencies.append(measured_latency)
            repeated = route_prompt(
                case["prompt"],
                index,
                taxonomy,
                runtime_roots=case_runtime_roots,
            )
        finally:
            if drift_directory is not None:
                drift_directory.cleanup()
        deterministic_replay_verified = canonical(route) == canonical(repeated)
        if deterministic_replay_verified:
            deterministic_matches += 1
        if observation_log is not None and fingerprint_key is not None:
            event = shadow_event(
                route,
                index,
                request_id=case["id"],
                baseline_skill=baseline_skill,
                latency_ms=measured_latency,
                evaluation_cohort=case_type,
                prompt_fingerprint=plan_by_id[case["id"]]["prompt_fingerprint"],
                deterministic_replay_verified=deterministic_replay_verified,
            )
            append_observation(observation_log, fingerprint_key, event)
        actual = [item["runtime_id"] for item in route.get("recommended", [])]
        failures: list[str] = []
        if case.get("expected_decision") and route["decision"] != case["expected_decision"]:
            failures.append("decision_mismatch")
        if case.get("expected_top") and (not actual or actual[0] != case["expected_top"]):
            failures.append("top_mismatch")
        for expected in case.get("expected_contains", []):
            if expected not in actual:
                failures.append("missing_expected")
        for forbidden in case.get("expected_excludes", []):
            if forbidden in actual:
                failures.append("forbidden_present")
        if "expected_safety_gate" in case and route["safety_gate_required"] is not case["expected_safety_gate"]:
            failures.append("safety_gate_mismatch")
        if route.get("authority_granted") is not False:
            failures.append("authority_expansion")
        if not deterministic_replay_verified:
            failures.append("nondeterministic_result")
        serialized = json.dumps(route, ensure_ascii=False)
        if case["prompt"] in serialized:
            failures.append("raw_prompt_leak")
        results.append(
            {
                "case_id": case["id"],
                "case_type": case_type,
                "passed": not failures,
                "failure_codes": sorted(set(failures)),
                "decision": route["decision"],
                "recommended_runtime_ids": actual,
                "safety_gate_required": bool(route["safety_gate_required"]),
                "fallback_used": bool(route["fallback_used"]),
                "clarification_required": bool(route["clarification_required"]),
                "reason_codes": list(route["reason_codes"]),
                "runtime_binding_verified": route.get("runtime_binding_verified"),
                "deterministic_replay_verified": deterministic_replay_verified,
            }
        )
    passed = sum(1 for result in results if result["passed"])
    total = len(results)

    def case_rate(case_type: str, predicate: Any) -> float:
        selected = [
            (case, result)
            for case, result in zip(cases, results)
            if case.get("case_type") == case_type
        ]
        if not selected:
            return 1.0
        return sum(1 for case, result in selected if predicate(case, result)) / len(selected)

    explicit_rate = case_rate("explicit", lambda _case, result: result["passed"])
    fallback_rate = case_rate(
        "fallback",
        lambda _case, result: result["passed"] and result["decision"] == "fallback",
    )
    single_top1 = case_rate(
        "single_domain",
        lambda case, result: bool(result["recommended_runtime_ids"])
        and result["recommended_runtime_ids"][0] == case.get("expected_top"),
    )
    multi_top3 = case_rate(
        "multi_domain",
        lambda case, result: all(
            item in result["recommended_runtime_ids"][:3]
            for item in case.get("expected_contains", [])
        ),
    )
    baseline = fixture.get("baseline_metrics", {})
    if not isinstance(baseline, dict):
        raise ValueError("fixture.baseline_metrics must be an object")
    route_accuracy = passed / total
    sorted_latencies = sorted(latencies)
    p95_position = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
    metrics = {
        "explicit_invocation_adherence": explicit_rate,
        "deterministic_order_rate": deterministic_matches / total,
        "fallback_success_rate": fallback_rate,
        "negative_control_misroutes": sum(
            1
            for case, result in zip(cases, results)
            if case.get("case_type") == "negative_control" and not result["passed"]
        ),
        "high_risk_misroutes": sum(
            1
            for case, result in zip(cases, results)
            if case.get("case_type") == "high_risk" and not result["passed"]
        ),
        "authority_expansion_events": sum(
            1 for result in results if "authority_expansion" in result["failure_codes"]
        ),
        "raw_prompt_leaks": sum(
            1 for result in results if "raw_prompt_leak" in result["failure_codes"]
        ),
        "tree_drift_misses": sum(
            1
            for case, result in zip(cases, results)
            if case.get("case_type") == "tree_drift" and not result["passed"]
        ),
        "valid_samples": total,
        "single_domain_top1": single_top1,
        "multi_domain_top3": multi_top3,
        "route_accuracy": route_accuracy,
        "baseline_route_accuracy": baseline.get("route_accuracy", route_accuracy),
        "manual_correction_rate": 1.0 - route_accuracy,
        "baseline_manual_correction_rate": baseline.get(
            "manual_correction_rate", 1.0 - route_accuracy
        ),
        "p95_router_latency_ms": round(sorted_latencies[p95_position], 3),
        "net_token_savings_status": fixture.get(
            "net_token_savings_status", "not_measurable"
        ),
        "host_deferred_loading_verified": fixture.get(
            "host_deferred_loading_verified", False
        ) is True,
        "fallback_verified": any(
            case.get("case_type") == "fallback" for case in cases
        ) and fallback_rate == 1.0,
        "registry_rollback_verified": fixture.get(
            "registry_rollback_verified", False
        ) is True,
    }
    case_ids = [result["case_id"] for result in results]
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
    report = {
        "schema_version": "skill-sublation.routing-fixture-report.v1",
        "fixture_id": fixture.get("fixture_id", "unnamed"),
        "sample_source": sample_source,
        "authorization_evidence": authorization_evidence,
        "index_sha256": index.get("content_sha256"),
        "case_id_set_sha256": sha256(sorted(case_ids)),
        "fixture_plan_sha256": plan["content_sha256"],
        "result_projection_sha256": sha256(
            sorted(result_projection, key=lambda item: item["case_id"])
        ),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": route_accuracy,
        "raw_prompt_leaks": metrics["raw_prompt_leaks"],
        "metrics": metrics,
        "results": results,
    }
    report_errors = validate_fixture_report(report)
    if report_errors:
        raise ValueError("; ".join(report_errors))
    return report


def _router_hash() -> str:
    return sha256(Path(__file__).read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taxonomy", type=Path, default=default_taxonomy_path())
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("metadata", nargs="+", type=Path)

    index_parser = sub.add_parser("index")
    index_parser.add_argument("--skills-root", action="append", default=[])
    index_parser.add_argument("--metadata", action="append", default=[], type=Path)
    # This release is deliberately shadow-only. A live CLI requires a separate
    # reviewed host-integration candidate and explicit user authorization.
    index_parser.add_argument("--mode", choices=("shadow",), default="shadow")
    index_parser.add_argument("--previous-index-sha256")
    index_parser.add_argument("--output", type=Path)

    route_parser = sub.add_parser("route")
    route_parser.add_argument("--index", required=True, type=Path)
    route_parser.add_argument("--prompt-file", default="-")
    route_parser.add_argument("--top-k", type=int)
    route_parser.add_argument("--skills-root", action="append", default=[])
    route_parser.add_argument("--observation-log", type=Path)
    route_parser.add_argument("--attestation-key", type=Path)
    route_parser.add_argument("--request-id", default=None)
    route_parser.add_argument("--baseline-skill")
    route_parser.add_argument(
        "--evaluation-cohort", choices=sorted(FIXTURE_CASE_TYPES), default="general"
    )

    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--skills-root", action="append", required=True)
    baseline_parser.add_argument(
        "--host-loading-mode",
        choices=("unknown", "full-injection", "deferred"),
        default="unknown",
    )
    baseline_parser.add_argument("--output", type=Path)

    suggest_parser = sub.add_parser("suggest")
    suggest_parser.add_argument("skill_root", type=Path)
    suggest_parser.add_argument("--output", type=Path)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--index", required=True, type=Path)
    plan_parser.add_argument("--fixture", required=True, type=Path)
    plan_parser.add_argument("--attestation-key", type=Path)
    plan_parser.add_argument("--output", required=True, type=Path)

    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--index", required=True, type=Path)
    evaluate_parser.add_argument("--fixture", required=True, type=Path)
    evaluate_parser.add_argument("--skills-root", action="append", default=[])
    evaluate_parser.add_argument("--attestation-key", type=Path)
    evaluate_parser.add_argument("--observation-log", type=Path)
    evaluate_parser.add_argument("--baseline-skill")
    evaluate_parser.add_argument(
        "--plan",
        "--supplied-plan",
        dest="supplied_plan",
        type=Path,
        help="pre-existing redacted plan to verify before any observation",
    )
    evaluate_parser.add_argument("--plan-output", type=Path)
    evaluate_parser.add_argument("--output", type=Path)

    verify_parser = sub.add_parser("verify-log")
    verify_parser.add_argument("--observation-log", required=True, type=Path)
    verify_parser.add_argument("--attestation-key", required=True, type=Path)
    verify_parser.add_argument("--key-id", required=True)
    verify_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    try:
        taxonomy = load_taxonomy(args.taxonomy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.command == "route":
            write_json(None, fallback_result("router_unavailable", errors=[str(exc)]))
            return 0
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "validate":
            failures: dict[str, list[str]] = {}
            for path in args.metadata:
                errors = validate_metadata(read_json(path), taxonomy)
                if errors:
                    failures[str(path)] = errors
            write_json(None, {"status": "failed" if failures else "passed", "failures": failures})
            return 2 if failures else 0
        if args.command == "index":
            roots = [parse_root_spec(value) for value in args.skills_root]
            index = build_index(
                roots,
                args.metadata,
                taxonomy,
                mode=args.mode,
                router_sha256=_router_hash(),
                previous_index_sha256=args.previous_index_sha256,
            )
            write_json(args.output, index)
            return 0
        if args.command == "route":
            try:
                index = read_json(args.index)
                prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8")
                started = time.perf_counter()
                roots = [parse_root_spec(value) for value in args.skills_root]
                route = route_prompt(
                    prompt,
                    index,
                    taxonomy,
                    top_k=args.top_k,
                    runtime_roots=roots if args.skills_root else None,
                )
                latency = (time.perf_counter() - started) * 1000
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                write_json(None, fallback_result("router_unavailable", errors=[str(exc)]))
                return 0
            deterministic_replay_verified = True
            if args.observation_log:
                if not args.attestation_key:
                    raise ValueError("--observation-log requires --attestation-key")
                repeated = route_prompt(
                    prompt,
                    index,
                    taxonomy,
                    top_k=args.top_k,
                    runtime_roots=roots if args.skills_root else None,
                )
                deterministic_replay_verified = canonical(route) == canonical(repeated)
            if args.observation_log:
                event = shadow_event(
                    route,
                    index,
                    request_id=args.request_id or uuid.uuid4().hex,
                    baseline_skill=args.baseline_skill,
                    latency_ms=latency,
                    evaluation_cohort=args.evaluation_cohort,
                    prompt_fingerprint=prompt_fingerprint(prompt, args.attestation_key),
                    deterministic_replay_verified=deterministic_replay_verified,
                )
                append_observation(args.observation_log, args.attestation_key, event)
            write_json(None, route)
            return 0
        if args.command == "baseline":
            roots = [parse_root_spec(value) for value in args.skills_root]
            write_json(args.output, catalog_baseline(roots, args.host_loading_mode))
            return 0
        if args.command == "suggest":
            write_json(args.output, suggest_metadata(args.skill_root.expanduser().resolve(), taxonomy))
            return 0
        if args.command == "plan":
            index = read_json(args.index)
            errors = validate_index(index, taxonomy)
            if errors:
                raise ValueError("; ".join(errors))
            for label, source_path in (
                ("index", args.index),
                ("fixture", args.fixture),
                ("attestation key", args.attestation_key),
            ):
                if paths_alias(args.output, source_path):
                    raise ValueError(f"plan output must not overwrite the {label}")
            write_json(
                args.output,
                redacted_fixture_plan(
                    index,
                    read_json(args.fixture),
                    fingerprint_key=args.attestation_key,
                ),
            )
            return 0
        if args.command == "evaluate":
            index = read_json(args.index)
            errors = validate_index(index, taxonomy)
            if errors:
                raise ValueError("; ".join(errors))
            fixture = read_json(args.fixture)
            sample_source = fixture.get("sample_source", "synthetic_fixture")
            if args.supplied_plan and args.plan_output:
                raise ValueError("--plan and --plan-output cannot be combined")
            for label, protected_path in (
                ("index", args.index),
                ("fixture", args.fixture),
                ("attestation key", args.attestation_key),
                ("supplied fixture plan", args.supplied_plan),
                ("observation log", args.observation_log),
            ):
                if paths_alias(args.output, protected_path):
                    raise ValueError(f"report output must not overwrite the {label}")
            if args.plan_output:
                if sample_source != "synthetic_fixture":
                    raise ValueError(
                        "--plan-output is limited to synthetic offline evaluation; use the plan command first"
                    )
                if args.observation_log:
                    raise ValueError(
                        "--plan-output cannot be combined with observation logging"
                    )
                if paths_alias(args.plan_output, args.output):
                    raise ValueError("report output must not overwrite --plan-output")
                for label, protected_path in (
                    ("index", args.index),
                    ("fixture", args.fixture),
                    ("attestation key", args.attestation_key),
                ):
                    if paths_alias(args.plan_output, protected_path):
                        raise ValueError(f"--plan-output must not overwrite the {label}")
                write_json(
                    args.plan_output,
                    redacted_fixture_plan(
                        index,
                        fixture,
                        fingerprint_key=args.attestation_key,
                    ),
                )
            supplied_plan = (
                read_existing_plan(args.supplied_plan)
                if args.supplied_plan is not None
                else None
            )
            roots = [parse_root_spec(value) for value in args.skills_root]
            report = evaluate_fixture(
                index,
                taxonomy,
                fixture,
                runtime_roots=roots if args.skills_root else None,
                fingerprint_key=args.attestation_key,
                observation_log=args.observation_log,
                baseline_skill=args.baseline_skill,
                supplied_plan=supplied_plan,
            )
            write_json(args.output, report)
            return 2 if report["failed"] else 0
        if args.command == "verify-log":
            write_json(
                args.output,
                verify_observation_log(
                    args.observation_log,
                    args.attestation_key,
                    key_id=args.key_id,
                ),
            )
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
