#!/usr/bin/env python3
"""Project-bound wake-intent gate for the Sublation V5 collaboration panel.

This module deliberately does not launch a process, invoke Claude/Hermes, read
credentials, or contact a network endpoint.  It has three layers only:

1. discover one eligible project-local message;
2. enforce window, ownership, lock, cooldown, quota, and de-duplication gates;
3. let an already registered host adapter claim the intent and return a
   structurally verified receipt.

The wake cursor advances only after ``sent_and_verified``.  Every authoritative
transition is appended to a SHA-256 hash chain.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hmac
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


POLICY_SCHEMA = "skill-sublation.agent-wakeup-policy.v1"
RECEIPT_SCHEMA = "skill-sublation.agent-wakeup-receipt.v1"
SNAPSHOT_SCHEMA = "skill-sublation.agent-wakeup-snapshot.v1"
BINDING_SCHEMA = "skill-sublation.agent-wakeup-binding.v1"
CURSORS_SCHEMA = "skill-sublation.agent-wakeup-cursors.v1"
EVENT_SCHEMA = "skill-sublation.agent-wakeup-event.v1"
INTENT_EVENT_SCHEMA = "skill-sublation.agent-wakeup-intent-event.v1"
CLOSE_RECEIPT_SCHEMA = "skill-sublation.agent-wakeup-close-receipt.v1"
COMPLETION_JOURNAL_SCHEMA = "skill-sublation.agent-wakeup-completion-journal.v1"
POLICY_REVISION = 1
LOOPBACK_HOST = "127.0.0.1"
MAX_RECEIPT_CLOCK_SKEW_SECONDS = 5
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHA_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
HMAC_RE = re.compile(r"^hmac-sha256:[a-f0-9]{64}$")
FORBIDDEN_KEYS = {
    "argv",
    "args",
    "command",
    "command_line",
    "env",
    "environment",
    "secret",
    "secrets",
    "token",
    "credential",
    "credentials",
    "password",
    "raw_prompt",
    "prompt",
    "message_body",
}


class WakeupError(RuntimeError):
    """Fail-closed wakeup contract error."""

    def __init__(self, message: str, *, code: str = "wakeup_error") -> None:
        super().__init__(message)
        self.code = code


class ProjectBindingError(WakeupError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="project_binding_mismatch")


class ChainError(WakeupError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="hash_chain_invalid")


class GateBlocked(WakeupError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    data = value if isinstance(value, bytes) else _canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WakeupError(f"{label} must be an RFC3339 timestamp", code="invalid_input")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WakeupError(f"{label} must be an RFC3339 timestamp", code="invalid_input") from exc
    if parsed.tzinfo is None:
        raise WakeupError(f"{label} must include a timezone", code="invalid_input")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WakeupError(f"cannot read JSON: {path}", code="state_unavailable") from exc
    if not isinstance(value, dict):
        raise WakeupError(f"expected JSON object: {path}", code="state_invalid")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WakeupError(f"cannot read event log: {path}", code="state_unavailable") from exc
    result: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainError(f"invalid JSONL at {path}:{number}") from exc
        if not isinstance(value, dict):
            raise ChainError(f"non-object JSONL record at {path}:{number}")
        result.append(value)
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    line = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _strict_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise WakeupError(
            f"{label} fields differ; extra={extra}, missing={missing}",
            code="contract_invalid",
        )


def _reject_dangerous_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_KEYS:
                raise WakeupError(
                    f"forbidden command, secret, or raw-content field at {path}.{key}",
                    code="unsafe_field",
                )
            _reject_dangerous_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_dangerous_fields(child, f"{path}[{index}]")


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise WakeupError(f"{label} is not a canonical opaque id", code="contract_invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise WakeupError(f"{label} is not a sha256 digest", code="contract_invalid")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WakeupError(
            f"{label} must be an integer from {minimum} through {maximum}",
            code="contract_invalid",
        )
    return value


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    payload.pop("receipt_hmac", None)
    return _sha256(payload)


def _hmac_sha256(key: bytes, value: Mapping[str, Any]) -> str:
    return "hmac-sha256:" + hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def load_host_receipt_key(path: str | os.PathLike[str]) -> bytes:
    """Host-coordinator helper; callers must sandbox Agents away from this path."""

    key_path = Path(path).expanduser()
    try:
        metadata = key_path.lstat()
    except OSError as exc:
        raise WakeupError("receipt HMAC key is unavailable", code="receipt_key_invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WakeupError("receipt HMAC key must be a regular non-symlink file", code="receipt_key_invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise WakeupError("receipt HMAC key permissions or owner are unsafe", code="receipt_key_invalid")
    value = key_path.read_bytes()
    if len(value) != 32:
        raise WakeupError("receipt HMAC key must contain exactly 32 bytes", code="receipt_key_invalid")
    return value


def _claim_proof_payload(intent: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "skill-sublation.agent-wakeup-claim-proof.v1",
        "project_id": intent["project_id"],
        "room_identity": intent["room_identity"],
        "intent_id": intent["intent_id"],
        "window_id": intent["window_id"],
        "policy_revision": intent["policy_revision"],
        "policy_sha256": intent["policy_sha256"],
        "seat_id": intent["seat_id"],
        "seat_generation": intent["seat_generation"],
        "adapter_id": intent["adapter_id"],
        "adapter_sha256": intent["adapter_sha256"],
        "key_id": intent["key_id"],
    }


def _policy_governance_payload(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "skill-sublation.agent-wakeup-policy-governance.v1",
        "project_id": binding["project_id"],
        "room_identity": binding["room_identity"],
        "policy_revision": binding["policy_revision"],
        "policy_sha256": binding["policy_sha256"],
        "key_id": binding["receipt_key_id"],
    }


def build_claim_proof(intent: Mapping[str, Any], receipt_key: bytes) -> str:
    """Build the proof in a host coordinator that is allowed to read the key."""

    if not isinstance(receipt_key, bytes) or len(receipt_key) != 32:
        raise WakeupError("host receipt key must be exactly 32 bytes", code="receipt_key_invalid")
    return _hmac_sha256(receipt_key, _claim_proof_payload(intent))


def _receipt_hmac(receipt: Mapping[str, Any], receipt_key: bytes) -> str:
    payload = dict(receipt)
    payload.pop("receipt_hmac", None)
    return _hmac_sha256(receipt_key, payload)


class WakeupStore:
    """Append-only wake-intent state bound to one collaboration-panel project."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        if not self.project_root.is_dir():
            raise WakeupError("project root is not an existing directory", code="project_unavailable")
        self.panel_dir = self.project_root / ".sublation-panel"
        self.panel_config_path = self.panel_dir / "config.json"
        self.panel_lock_path = self.panel_dir / ".lock"
        self.roster_path = self.panel_dir / "roster.json"
        self.roster_events_path = self.panel_dir / "roster-events.jsonl"
        self.messages_path = self.panel_dir / "messages.jsonl"
        self.panel_cursors_dir = self.panel_dir / "cursors"
        self._final_state_dir = self.panel_dir / "wakeup"
        self._set_state_dir(self._final_state_dir)
        self._clock = clock or _utc_now

    def _set_state_dir(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.binding_path = self.state_dir / "binding.json"
        self.policy_path = self.state_dir / "policy.json"
        self.cursors_path = self.state_dir / "cursors.json"
        self.events_path = self.state_dir / "events.jsonl"
        self.intents_path = self.state_dir / "intents.jsonl"
        self.completion_journal_path = self.state_dir / "completion-journal.json"
        self.lock_path = self.state_dir / ".lock"
        self.receipt_key_path = self.state_dir / ".receipt-key"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise WakeupError("clock must return a timezone-aware datetime", code="clock_invalid")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _wakeup_locked(self) -> Iterator[None]:
        if not self.state_dir.is_dir():
            raise WakeupError("wakeup state is not initialized", code="not_initialized")
        descriptor = os.open(self.lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GateBlocked("wakeup gate lock is busy", code="lock_busy") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def _panel_locked(self) -> Iterator[None]:
        if not self.panel_dir.is_dir():
            raise WakeupError("collaboration panel is not initialized", code="panel_not_initialized")
        try:
            panel_descriptor = os.open(self.panel_lock_path, os.O_RDWR)
        except OSError as exc:
            raise WakeupError("panel ledger lock is unavailable", code="panel_state_unavailable") from exc
        try:
            try:
                fcntl.flock(panel_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GateBlocked("panel ledger lock is busy", code="panel_lock_busy") from exc
            yield
        finally:
            try:
                fcntl.flock(panel_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(panel_descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire the only supported cross-ledger order: panel, then wakeup."""

        with self._panel_locked():
            with self._wakeup_locked():
                yield

    def _live_binding(self) -> dict[str, Any]:
        config = _read_json(self.panel_config_path)
        roster = _read_json(self.roster_path)
        expected_root = str(self.project_root)
        if config.get("project_root") != expected_root:
            raise ProjectBindingError("panel config project_root differs from this project")
        if config.get("host") != LOOPBACK_HOST:
            raise ProjectBindingError("agent wakeup requires the 127.0.0.1 room")
        project_id = _opaque(config.get("project_id"), "project_id")
        if roster.get("project_id") != project_id:
            raise ProjectBindingError("roster and panel config project_id differ")
        port = config.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ProjectBindingError("panel config port is invalid")
        identity_payload = {
            "project_id": project_id,
            "project_root": expected_root,
            "host": LOOPBACK_HOST,
            "port": port,
        }
        return {
            "schema_version": BINDING_SCHEMA,
            **identity_payload,
            "room_identity": _sha256(identity_payload),
        }

    def _verify_binding(self) -> dict[str, Any]:
        live = self._live_binding()
        stored = _read_json(self.binding_path)
        for key in ("schema_version", "project_id", "project_root", "host", "port", "room_identity"):
            if stored.get(key) != live.get(key):
                raise ProjectBindingError(f"stored wakeup binding differs at {key}")
        return stored

    def _receipt_key(self) -> tuple[bytes, dict[str, Any]]:
        binding = _read_json(self.binding_path)
        key = load_host_receipt_key(self.receipt_key_path)
        if _sha256(key) != binding.get("receipt_key_sha256"):
            raise WakeupError("receipt HMAC key digest differs from binding", code="receipt_key_invalid")
        _opaque(binding.get("receipt_key_id"), "receipt_key_id")
        return key, binding

    def _load_completion_journal(self) -> dict[str, Any]:
        journal = _read_json(self.completion_journal_path)
        if journal.get("schema_version") != COMPLETION_JOURNAL_SCHEMA:
            raise ChainError("completion journal schema is invalid")
        claimed = journal.get("journal_sha256")
        unsigned = dict(journal)
        unsigned.pop("journal_sha256", None)
        if not isinstance(claimed, str) or claimed != _sha256(unsigned):
            raise ChainError("completion journal sha256 mismatch")
        if not isinstance(journal.get("entries"), dict):
            raise ChainError("completion journal entries are invalid")
        return journal

    def _write_completion_journal(self, journal: Mapping[str, Any]) -> None:
        value = copy.deepcopy(dict(journal))
        value.pop("journal_sha256", None)
        value["journal_sha256"] = _sha256(value)
        _atomic_json(self.completion_journal_path, value)

    def _incomplete_completions(self) -> dict[str, dict[str, Any]]:
        journal = self._load_completion_journal()
        return {
            intent_id: copy.deepcopy(entry)
            for intent_id, entry in journal["entries"].items()
            if isinstance(entry, dict) and entry.get("state") != "committed"
        }

    def _assert_no_incomplete_completion(self) -> None:
        incomplete = self._incomplete_completions()
        if incomplete:
            raise GateBlocked(
                "an interrupted completion must be replayed before other mutations",
                code="completion_recovery_required",
            )

    def _fault_hook(self, stage: str) -> None:
        """No-op production hook used by deterministic crash-recovery tests."""

        del stage

    @staticmethod
    def _validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
        _reject_dangerous_fields(policy)
        _strict_keys(
            policy,
            {
                "schema_version",
                "binding_source",
                "window_policy",
                "retry_policy",
                "default_policy",
                "registered_adapters",
                "pending_host_installs",
                "seat_policies",
            },
            "policy",
        )
        if policy.get("schema_version") != POLICY_SCHEMA:
            raise WakeupError("unsupported wakeup policy schema", code="contract_invalid")
        if policy.get("binding_source") != "project_local_panel_config":
            raise WakeupError("policy must derive binding from panel config", code="contract_invalid")

        window = policy.get("window_policy")
        if not isinstance(window, Mapping):
            raise WakeupError("window_policy must be an object", code="contract_invalid")
        _strict_keys(
            window,
            {
                "default_state",
                "requires_explicit_user_arm",
                "maximum_duration_seconds",
                "maximum_quiet_cycles",
                "maximum_wakes",
                "drain_on_any_limit",
                "close_requires_no_pending_intents",
                "close_requires_no_active_lease",
            },
            "window_policy",
        )
        required_window = {
            "default_state": "closed",
            "requires_explicit_user_arm": True,
            "drain_on_any_limit": True,
            "close_requires_no_pending_intents": True,
            "close_requires_no_active_lease": True,
        }
        if any(window.get(key) != expected for key, expected in required_window.items()):
            raise WakeupError("wake window safety constants cannot be relaxed", code="contract_invalid")
        _bounded_int(window.get("maximum_duration_seconds"), "maximum_duration_seconds", 60, 86400)
        _bounded_int(window.get("maximum_quiet_cycles"), "maximum_quiet_cycles", 1, 1000)
        _bounded_int(window.get("maximum_wakes"), "maximum_wakes", 1, 1000)

        retry = policy.get("retry_policy")
        if not isinstance(retry, Mapping):
            raise WakeupError("retry_policy must be an object", code="contract_invalid")
        _strict_keys(retry, {"backoff_seconds", "max_attempts", "terminal_outcome"}, "retry_policy")
        if retry.get("backoff_seconds") != [60, 180, 600]:
            raise WakeupError("retry backoff must be exactly 60/180/600 seconds", code="contract_invalid")
        if retry.get("max_attempts") != 4 or retry.get("terminal_outcome") != "terminal_hold":
            raise WakeupError("retry attempts must terminate at terminal_hold", code="contract_invalid")

        default = policy.get("default_policy")
        if not isinstance(default, Mapping):
            raise WakeupError("default_policy must be an object", code="contract_invalid")
        WakeupStore._validate_seat_policy(default, default_policy=True)

        raw_adapters = policy.get("registered_adapters")
        if not isinstance(raw_adapters, list) or len(raw_adapters) > 32:
            raise WakeupError("registered_adapters must be an array of at most 32", code="contract_invalid")
        adapters: dict[str, dict[str, Any]] = {}
        for item in raw_adapters:
            if not isinstance(item, Mapping):
                raise WakeupError("adapter registration must be an object", code="contract_invalid")
            _strict_keys(item, {"adapter_id", "adapter_sha256", "kind", "allowed_seat_ids"}, "adapter")
            adapter_id = _opaque(item.get("adapter_id"), "adapter_id")
            if adapter_id in adapters:
                raise WakeupError("duplicate adapter_id", code="contract_invalid")
            _digest(item.get("adapter_sha256"), "adapter_sha256")
            if item.get("kind") not in {"watcher_auto", "local_wakeup", "task_poll"}:
                raise WakeupError("adapter kind is not allowed", code="contract_invalid")
            allowed = item.get("allowed_seat_ids")
            if not isinstance(allowed, list) or not 1 <= len(allowed) <= 32:
                raise WakeupError("allowed_seat_ids must contain 1 to 32 ids", code="contract_invalid")
            normalized = [_opaque(value, "allowed seat id") for value in allowed]
            if len(set(normalized)) != len(normalized):
                raise WakeupError("duplicate allowed seat id", code="contract_invalid")
            adapters[adapter_id] = copy.deepcopy(dict(item))

        pending_installs = policy.get("pending_host_installs")
        if not isinstance(pending_installs, list) or len(pending_installs) > 32:
            raise WakeupError("pending_host_installs must be an array of at most 32", code="contract_invalid")
        pending_pairs: set[tuple[str, str]] = set()
        for item in pending_installs:
            if not isinstance(item, Mapping):
                raise WakeupError("pending host install must be an object", code="contract_invalid")
            _strict_keys(item, {"seat_id", "adapter_id", "kind", "status"}, "pending host install")
            pair = (_opaque(item.get("seat_id"), "pending seat id"), _opaque(item.get("adapter_id"), "pending adapter id"))
            if pair in pending_pairs or item.get("status") != "pending_host_install":
                raise WakeupError("pending host install is duplicate or not pending", code="contract_invalid")
            if item.get("kind") not in {"watcher_auto", "local_wakeup", "task_poll"}:
                raise WakeupError("pending host adapter kind is not allowed", code="contract_invalid")
            if pair[1] in adapters:
                raise WakeupError("adapter cannot be registered and pending simultaneously", code="contract_invalid")
            pending_pairs.add(pair)

        raw_seats = policy.get("seat_policies")
        if not isinstance(raw_seats, list) or len(raw_seats) > 32:
            raise WakeupError("seat_policies must be an array of at most 32", code="contract_invalid")
        seat_ids: set[str] = set()
        for item in raw_seats:
            if not isinstance(item, Mapping):
                raise WakeupError("seat policy must be an object", code="contract_invalid")
            WakeupStore._validate_seat_policy(item, default_policy=False)
            seat_id = _opaque(item.get("seat_id"), "seat_id")
            if seat_id in seat_ids or seat_id == "user":
                raise WakeupError("duplicate or user seat policy", code="contract_invalid")
            seat_ids.add(seat_id)
            if item.get("enabled") is True:
                adapter_id = str(item.get("adapter_id"))
                adapter = adapters.get(adapter_id)
                if adapter is None or seat_id not in adapter["allowed_seat_ids"]:
                    raise WakeupError("enabled seat lacks an owning registered adapter", code="contract_invalid")
        return copy.deepcopy(dict(policy))

    @staticmethod
    def _validate_seat_policy(policy: Mapping[str, Any], *, default_policy: bool) -> None:
        keys = {
            "enabled",
            "mode",
            "cooldown_seconds",
            "hourly_limit",
            "timeout_seconds",
            "escalation_seat_id",
            "max_messages_per_wake",
            "silent_when_idle",
        }
        if not default_policy:
            keys |= {"seat_id", "adapter_id"}
        _strict_keys(policy, keys, "default policy" if default_policy else "seat policy")
        enabled = policy.get("enabled")
        if not isinstance(enabled, bool):
            raise WakeupError("enabled must be boolean", code="contract_invalid")
        mode = policy.get("mode")
        adapter_id = policy.get("adapter_id") if not default_policy else None
        if default_policy:
            if enabled or mode != "queue_only":
                raise WakeupError("dynamic-seat default must be disabled and queue_only", code="contract_invalid")
        elif enabled:
            if mode != "intent_only":
                raise WakeupError("enabled seat must be intent_only", code="contract_invalid")
            _opaque(adapter_id, "adapter_id")
        elif mode != "queue_only" or adapter_id is not None:
            raise WakeupError("disabled seat must be queue_only with null adapter", code="contract_invalid")
        _bounded_int(policy.get("cooldown_seconds"), "cooldown_seconds", 1, 3600)
        _bounded_int(policy.get("hourly_limit"), "hourly_limit", 1, 60)
        _bounded_int(policy.get("timeout_seconds"), "timeout_seconds", 5, 3600)
        if policy.get("escalation_seat_id") != "user":
            raise WakeupError("timeout escalation must target user", code="contract_invalid")
        if policy.get("max_messages_per_wake") != 1:
            raise WakeupError("each wake may contain exactly one message id", code="contract_invalid")
        if policy.get("silent_when_idle") is not True:
            raise WakeupError("idle wakeup must remain silent", code="contract_invalid")

    def initialize(self, policy: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
        if self._final_state_dir.exists():
            raise WakeupError("wakeup state is already initialized", code="already_initialized")
        if isinstance(policy, Mapping):
            normalized_policy = self._validate_policy(policy)
        else:
            normalized_policy = self._validate_policy(_read_json(Path(policy).expanduser().resolve()))
        staging = self.panel_dir / (".wakeup-staging-" + uuid.uuid4().hex)
        with self._panel_locked():
            if self._final_state_dir.exists():
                raise WakeupError("wakeup state is already initialized", code="already_initialized")
            live = self._live_binding()
            roster = _read_json(self.roster_path)
            current_seats = {
                item.get("id")
                for item in roster.get("seats", [])
                if isinstance(item, Mapping) and item.get("kind") == "agent"
            }
            for seat_policy in normalized_policy["seat_policies"]:
                if seat_policy["seat_id"] not in current_seats:
                    raise WakeupError(
                        f"explicit wake policy seat does not exist: {seat_policy['seat_id']}",
                        code="contract_invalid",
                    )
            self._set_state_dir(staging)
            try:
                self.state_dir.mkdir(mode=0o700)
                for path in (self.events_path, self.intents_path):
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(descriptor)
                receipt_key = secrets.token_bytes(32)
                key_descriptor = os.open(
                    self.receipt_key_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(key_descriptor, receipt_key)
                    os.fsync(key_descriptor)
                finally:
                    os.close(key_descriptor)
                binding = {
                    **live,
                    "initialized_at": _iso(self._now()),
                    "policy_revision": POLICY_REVISION,
                    "policy_sha256": _sha256(normalized_policy),
                    "receipt_key_id": "wakeup-key-" + uuid.uuid4().hex,
                    "receipt_key_sha256": _sha256(receipt_key),
                }
                binding["policy_hmac"] = _hmac_sha256(
                    receipt_key,
                    _policy_governance_payload(binding),
                )
                _atomic_json(self.binding_path, binding)
                _atomic_json(self.policy_path, normalized_policy)
                cursors = {
                    "schema_version": CURSORS_SCHEMA,
                    "project_id": live["project_id"],
                    "seats": {},
                }
                _atomic_json(self.cursors_path, cursors)
                self._write_completion_journal(
                    {
                        "schema_version": COMPLETION_JOURNAL_SCHEMA,
                        "project_id": live["project_id"],
                        "entries": {},
                    }
                )
                with self._wakeup_locked():
                    self._append_event("initialize", "user", {"default_window_state": "closed"})
                    self._sync_seats(roster=roster, messages=self._messages())
                self._publish_staging(staging)
            except Exception:
                self._set_state_dir(self._final_state_dir)
                if staging.exists():
                    shutil.rmtree(staging)
                raise
            self._set_state_dir(self._final_state_dir)
            initialized_snapshot = self.snapshot(panel_lock_held=True)
        return initialized_snapshot

    def _publish_staging(self, staging: Path) -> None:
        if self._final_state_dir.exists():
            raise WakeupError("wakeup state appeared during initialization", code="already_initialized")
        os.replace(staging, self._final_state_dir)

    def _policy(self) -> dict[str, Any]:
        policy = self._validate_policy(_read_json(self.policy_path))
        binding = _read_json(self.binding_path)
        if binding.get("policy_revision") != POLICY_REVISION:
            raise GateBlocked("wakeup policy revision is not approved", code="policy_drift")
        if binding.get("policy_sha256") != _sha256(policy):
            raise GateBlocked("wakeup policy differs from its initialized digest", code="policy_drift")
        receipt_key, key_binding = self._receipt_key()
        expected_hmac = _hmac_sha256(
            receipt_key,
            _policy_governance_payload(key_binding),
        )
        claimed_hmac = key_binding.get("policy_hmac")
        if not isinstance(claimed_hmac, str) or not hmac.compare_digest(
            claimed_hmac,
            expected_hmac,
        ):
            raise GateBlocked("wakeup policy governance HMAC is invalid", code="policy_drift")
        return policy

    def _seat_policy(self, policy: Mapping[str, Any], seat_id: str) -> dict[str, Any]:
        for item in policy["seat_policies"]:
            if item["seat_id"] == seat_id:
                return copy.deepcopy(item)
        result = copy.deepcopy(policy["default_policy"])
        result.update({"seat_id": seat_id, "adapter_id": None})
        return result

    def _adapter(self, policy: Mapping[str, Any], adapter_id: str) -> dict[str, Any]:
        for item in policy["registered_adapters"]:
            if item["adapter_id"] == adapter_id:
                return copy.deepcopy(item)
        raise GateBlocked("adapter is not registered", code="adapter_unauthorized")

    def _verify_chain(self, path: Path, hash_field: str) -> list[dict[str, Any]]:
        records = _read_jsonl(path)
        previous: str | None = None
        for position, record in enumerate(records, 1):
            if record.get("previous_hash") != previous:
                raise ChainError(f"hash chain previous_hash mismatch at {path.name}:{position}")
            claimed = record.get(hash_field)
            payload = dict(record)
            payload.pop(hash_field, None)
            if not isinstance(claimed, str) or claimed != _sha256(payload):
                raise ChainError(f"hash chain digest mismatch at {path.name}:{position}")
            previous = claimed
        return records

    def _append_chained(self, path: Path, record: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
        records = self._verify_chain(path, hash_field)
        value = dict(record)
        value["previous_hash"] = records[-1][hash_field] if records else None
        value[hash_field] = _sha256(value)
        _append_jsonl(path, value)
        return value

    def _append_event(self, action: str, seat_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        binding = _read_json(self.binding_path)
        return self._append_chained(
            self.events_path,
            {
                "schema_version": EVENT_SCHEMA,
                "event_id": "wake-event-" + uuid.uuid4().hex,
                "timestamp": _iso(self._now()),
                "project_id": binding["project_id"],
                "room_identity": binding["room_identity"],
                "action": action,
                "seat_id": seat_id,
                "payload": copy.deepcopy(dict(payload)),
            },
            "event_hash",
        )

    def _append_intent(self, action: str, intent: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._append_chained(
            self.intents_path,
            {
                "schema_version": INTENT_EVENT_SCHEMA,
                "intent_event_id": "wake-intent-event-" + uuid.uuid4().hex,
                "timestamp": _iso(self._now()),
                "project_id": intent["project_id"],
                "room_identity": intent["room_identity"],
                "action": action,
                "intent_id": intent["intent_id"],
                "seat_id": intent["seat_id"],
                "payload": copy.deepcopy(dict(payload)),
            },
            "record_hash",
        )

    @staticmethod
    def _transition_event_projection(record: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """Return the deterministic event projection for retryable dual writes."""

        action = record.get("action")
        payload = record.get("payload") or {}
        intent_id = record["intent_id"]
        intent_seat = record["seat_id"]
        if action == "created":
            return (
                "wake_intent_created",
                intent_seat,
                {
                    "window_id": payload["window_id"],
                    "intent_id": intent_id,
                    "source_message_id": payload["source_message_id"],
                    "idempotency_key": payload["idempotency_key"],
                    "attempt": payload["attempt"],
                    "max_messages": payload["max_messages"],
                },
            )
        if action == "claimed":
            return (
                "wake_intent_claimed",
                intent_seat,
                {"intent_id": intent_id, **copy.deepcopy(dict(payload))},
            )
        if action == "user_retry_authorized":
            return (
                "wake_hold_disposition",
                "user",
                {
                    "intent_id": intent_id,
                    "seat_id": intent_seat,
                    **copy.deepcopy(dict(payload)),
                },
            )
        if action == "timed_out":
            return (
                "intent_timed_out",
                intent_seat,
                {"intent_id": intent_id, **copy.deepcopy(dict(payload))},
            )
        if action == "terminal_hold":
            return (
                "wake_terminal_hold",
                intent_seat,
                {"intent_id": intent_id, **copy.deepcopy(dict(payload))},
            )
        if action == "dual_consumer_hold":
            return (
                "wake_dual_consumer_hold",
                intent_seat,
                {"intent_id": intent_id, **copy.deepcopy(dict(payload))},
            )
        raise ChainError(f"intent transition has no retryable event projection: {action}")

    def _transition_projection_matches(
        self,
        event: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> bool:
        event_action, event_seat, payload = self._transition_event_projection(record)
        return (
            event.get("action") == event_action
            and event.get("seat_id") == event_seat
            and event.get("payload") == payload
        )

    def _transition_record(self, intent_id: str, action: str) -> dict[str, Any]:
        records = [
            record
            for record in self._verify_chain(self.intents_path, "record_hash")
            if record.get("intent_id") == intent_id and record.get("action") == action
        ]
        if len(records) != 1:
            raise ChainError(f"expected one {action} record for {intent_id}")
        return records[0]

    def _ensure_transition_event(self, record: Mapping[str, Any]) -> bool:
        events = self._verify_chain(self.events_path, "event_hash")
        matches = [
            event
            for event in events
            if self._transition_projection_matches(event, record)
        ]
        if len(matches) > 1:
            raise ChainError("intent transition event projection is duplicated")
        if matches:
            return False
        event_action, event_seat, payload = self._transition_event_projection(record)
        self._append_event(event_action, event_seat, payload)
        return True

    def _repair_transition_event_splits(self) -> set[tuple[str, str]]:
        """Repair only deterministic projections; never create intent authority."""

        repaired: set[tuple[str, str]] = set()
        for record in self._verify_chain(self.intents_path, "record_hash"):
            action = record.get("action")
            if action not in {
                "created",
                "claimed",
                "user_retry_authorized",
                "timed_out",
                "terminal_hold",
                "dual_consumer_hold",
            }:
                continue
            if self._ensure_transition_event(record):
                repaired.add((record["intent_id"], str(action)))
        return repaired

    def _validate_transition_event_projections(self) -> None:
        events = self._verify_chain(self.events_path, "event_hash")
        for record in self._verify_chain(self.intents_path, "record_hash"):
            if record.get("action") not in {
                "created",
                "claimed",
                "user_retry_authorized",
                "timed_out",
                "terminal_hold",
                "dual_consumer_hold",
            }:
                continue
            matches = [
                event
                for event in events
                if self._transition_projection_matches(event, record)
            ]
            if len(matches) != 1:
                raise ChainError(
                    "intent/event audit projection is split or duplicated for "
                    f"{record['intent_id']}:{record['action']}"
                )

    def _messages(self) -> list[dict[str, Any]]:
        raw = _read_jsonl(self.messages_path)
        unique: list[dict[str, Any]] = []
        by_id: dict[str, bytes] = {}
        for message in raw:
            message_id = _opaque(message.get("id"), "message id")
            encoded = _canonical(message)
            if message_id in by_id:
                if by_id[message_id] != encoded:
                    raise WakeupError("conflicting duplicate message id", code="message_log_invalid")
                continue
            by_id[message_id] = encoded
            unique.append(message)
        return unique

    def _message_position(self, message_id: str | None) -> int:
        if message_id is None:
            return -1
        for index, message in enumerate(self._messages()):
            if message.get("id") == message_id:
                return index
        raise ChainError(f"cursor references missing message: {message_id}")

    def _panel_cursor_path(self, seat_id: str) -> Path:
        _opaque(seat_id, "panel cursor seat id")
        return self.panel_cursors_dir / f"{seat_id}.json"

    def guard_panel_ack(
        self,
        *,
        seat_id: str,
        message_id: str,
        panel_lock_held: bool = False,
    ) -> dict[str, Any]:
        """Fail closed when a legacy/manual ACK could race a wake lease."""

        _opaque(seat_id, "seat_id")
        _opaque(message_id, "message_id")
        guard = self._wakeup_locked() if panel_lock_held else self._locked()
        with guard:
            self._verify_binding()
            self._policy()
            self._assert_no_incomplete_completion()
            states = self._intent_states()
            leases = [
                item
                for item in states.values()
                if item.get("seat_id") == seat_id
                and item.get("status") in {"pending", "claimed"}
            ]
            if leases:
                source_ids = sorted({str(item["source_message_id"]) for item in leases})
                raise GateBlocked(
                    "ordinary ACK is excluded while a wake intent or lease is open "
                    f"for source(s) {source_ids}",
                    code="wake_lease_conflict",
                )
            return {"allowed": True, "seat_id": seat_id, "message_id": message_id}

    def _roster_agents(self, roster: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        value = dict(roster) if roster is not None else _read_json(self.roster_path)
        result = []
        for item in value.get("seats", []):
            if isinstance(item, Mapping) and item.get("kind") == "agent":
                result.append(copy.deepcopy(dict(item)))
        return result

    def _seat_generations(self) -> dict[str, str]:
        """Return each seat's latest verified roster-event hash."""

        events = _read_jsonl(self.roster_events_path)
        previous: str | None = None
        generations: dict[str, str] = {}
        for position, event in enumerate(events, 1):
            if event.get("previous_event_sha256") != previous:
                raise ChainError(f"roster event chain mismatch at record {position}")
            claimed = event.get("event_sha256")
            unsigned = dict(event)
            unsigned.pop("event_sha256", None)
            if not isinstance(claimed, str) or claimed != _sha256(unsigned):
                raise ChainError(f"roster event digest mismatch at record {position}")
            seat_id = _opaque(event.get("seat_id"), "roster event seat id")
            generations[seat_id] = claimed
            previous = claimed
        return generations

    def _revalidate_intent_binding(
        self,
        intent: Mapping[str, Any],
        intents: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Recheck roster and window identity while both ledgers are locked."""

        seat_id = _opaque(intent.get("seat_id"), "intent seat id")
        policy = self._policy()
        binding = _read_json(self.binding_path)
        if (
            intent.get("policy_revision") != binding.get("policy_revision")
            or intent.get("policy_sha256") != binding.get("policy_sha256")
            or binding.get("policy_sha256") != _sha256(policy)
        ):
            raise GateBlocked("intent policy binding is stale", code="policy_drift")
        roster = _read_json(self.roster_path)
        seat = next(
            (
                item
                for item in self._roster_agents(roster)
                if item.get("id") == seat_id
            ),
            None,
        )
        if seat is None or seat.get("active") is not True or seat.get("retired_at") is not None:
            raise GateBlocked("intent seat is retired or inactive", code="seat_inactive")
        generation = self._seat_generations().get(seat_id)
        if generation is None or intent.get("seat_generation") != generation:
            raise GateBlocked("intent seat generation is stale", code="seat_generation_stale")
        events = self._verify_chain(self.events_path, "event_hash")
        window = self._window(events, intents)
        if window.get("window_id") != intent.get("window_id"):
            raise GateBlocked("intent belongs to a different wake window", code="window_binding_stale")
        if window.get("state") not in {"active", "draining"}:
            raise GateBlocked("intent wake window is no longer open", code="window_binding_stale")
        if window.get("armed_seat_generations", {}).get(seat_id) != generation:
            raise GateBlocked("wake window seat generation is stale", code="seat_generation_stale")
        if (
            window.get("policy_revision") != intent.get("policy_revision")
            or window.get("policy_sha256") != intent.get("policy_sha256")
        ):
            raise GateBlocked("wake window policy binding is stale", code="policy_drift")
        return window

    def _sync_seats(
        self,
        *,
        roster: Mapping[str, Any] | None = None,
        messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[str]:
        cursors = _read_json(self.cursors_path)
        seats = cursors.get("seats")
        if not isinstance(seats, dict):
            raise WakeupError("wakeup cursor projection is invalid", code="state_invalid")
        baseline = messages[-1]["id"] if messages else None
        generations = self._seat_generations()
        added: list[str] = []
        for seat in self._roster_agents(roster):
            seat_id = _opaque(seat.get("id"), "roster seat id")
            if seat_id in seats:
                continue
            seats[seat_id] = {
                "last_verified_message_id": baseline,
                "roster_generation": generations.get(seat_id),
                "first_seen_at": _iso(self._now()),
                "last_outcome": "baselined",
            }
            self._append_event(
                "seat_baselined",
                seat_id,
                {
                    "cursor_message_id": baseline,
                    "seat_generation": generations.get(seat_id),
                    "historical_backlog_inherited": False,
                    "enabled_by_default": False,
                    "default_mode": "queue_only",
                },
            )
            added.append(seat_id)
        if added:
            _atomic_json(self.cursors_path, cursors)
        return added

    def _intent_states(self) -> dict[str, dict[str, Any]]:
        records = self._verify_chain(self.intents_path, "record_hash")
        states: dict[str, dict[str, Any]] = {}
        for record in records:
            intent_id = record["intent_id"]
            action = record["action"]
            if action == "created":
                if intent_id in states:
                    raise ChainError("duplicate intent creation")
                states[intent_id] = {**record["payload"], "status": "pending", "created_at": record["timestamp"]}
            elif intent_id not in states:
                raise ChainError("intent transition precedes creation")
            elif action == "claimed":
                if states[intent_id]["status"] != "pending":
                    raise ChainError("intent claim has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = "claimed"
            elif action == "completed":
                if states[intent_id]["status"] != "claimed":
                    raise ChainError("intent completion has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = record["payload"]["outcome"]
            elif action == "timed_out":
                if states[intent_id]["status"] not in {"pending", "claimed"}:
                    raise ChainError("intent timeout has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = "root_hold"
            elif action == "terminal_hold":
                if states[intent_id]["status"] != "failed":
                    raise ChainError("terminal hold has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = "terminal_hold"
            elif action == "dual_consumer_hold":
                if states[intent_id]["status"] != "claimed":
                    raise ChainError("dual-consumer hold has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = "dual_consumer_hold"
            elif action == "user_retry_authorized":
                if states[intent_id]["status"] not in {
                    "root_hold",
                    "terminal_hold",
                    "dual_consumer_hold",
                }:
                    raise ChainError("user retry disposition has invalid predecessor")
                states[intent_id].update(record["payload"])
                states[intent_id]["status"] = "user_retry_authorized"
            else:
                raise ChainError(f"unsupported intent transition: {action}")
        return states

    def _window(self, events: Sequence[Mapping[str, Any]], intents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        window: dict[str, Any] = {
            "state": "closed",
            "window_id": None,
            "deadline": None,
            "quiet_cycles": 0,
            "max_quiet_cycles": None,
            "wakes": 0,
            "max_wakes": None,
            "pending_intents": 0,
            "active_leases": 0,
            "close_receipt_sha256": None,
            "armed_seat_generations": {},
            "policy_revision": None,
            "policy_sha256": None,
        }
        for event in events:
            action = event.get("action")
            payload = event.get("payload") or {}
            if action == "window_armed":
                window.update(
                    {
                        "state": "active",
                        "window_id": payload["window_id"],
                        "deadline": payload["deadline"],
                        "quiet_cycles": 0,
                        "max_quiet_cycles": payload["max_quiet_cycles"],
                        "wakes": 0,
                        "max_wakes": payload["max_wakes"],
                        "close_receipt_sha256": None,
                        "armed_seat_generations": copy.deepcopy(payload["armed_seat_generations"]),
                        "policy_revision": payload["policy_revision"],
                        "policy_sha256": payload["policy_sha256"],
                    }
                )
            elif payload.get("window_id") != window.get("window_id"):
                continue
            elif action == "quiet_cycle":
                window["quiet_cycles"] += 1
            elif action == "quiet_cycle_reset":
                window["quiet_cycles"] = 0
            elif action == "wake_intent_created":
                window["wakes"] += 1
            elif action == "window_draining":
                window["state"] = "draining"
                window["drain_reason"] = payload["reason"]
            elif action == "window_closed":
                window["state"] = "closed"
                window["close_receipt_sha256"] = payload["close_receipt_sha256"]
        if window["window_id"] is not None:
            current = [
                item
                for item in intents.values()
                if item.get("window_id") == window["window_id"]
                and item.get("status") in {"pending", "claimed"}
            ]
            window["pending_intents"] = len(current)
            window["active_leases"] = sum(item.get("status") == "claimed" for item in current)
        return window

    def arm_window(
        self,
        *,
        deadline: str,
        quiet_cycles: int,
        max_wakes: int,
        actor: str = "user",
    ) -> dict[str, Any]:
        if actor != "user":
            raise GateBlocked("only the user may arm a wake window", code="user_arm_required")
        with self._locked():
            self._verify_binding()
            self._assert_no_incomplete_completion()
            policy = self._policy()
            events = self._verify_chain(self.events_path, "event_hash")
            intents = self._intent_states()
            current = self._window(events, intents)
            if current["state"] != "closed" or current["pending_intents"]:
                raise GateBlocked("previous wake window is not fully closed", code="window_not_closed")
            now = self._now()
            parsed_deadline = _parse_time(deadline, "deadline")
            duration = (parsed_deadline - now).total_seconds()
            maximum_duration = policy["window_policy"]["maximum_duration_seconds"]
            if duration <= 0 or duration > maximum_duration:
                raise GateBlocked("wake deadline exceeds policy or is not in the future", code="window_deadline_invalid")
            maximum_quiet = policy["window_policy"]["maximum_quiet_cycles"]
            maximum_wakes = policy["window_policy"]["maximum_wakes"]
            _bounded_int(quiet_cycles, "quiet_cycles", 1, maximum_quiet)
            _bounded_int(max_wakes, "max_wakes", 1, maximum_wakes)
            window_id = "wake-window-" + uuid.uuid4().hex
            roster = _read_json(self.roster_path)
            generations = self._seat_generations()
            active_agents = [seat for seat in self._roster_agents(roster) if seat.get("active") is True]
            armed_generations = {
                seat["id"]: generations[seat["id"]]
                for seat in active_agents
                if seat["id"] in generations
            }
            previous_generations = current.get("armed_seat_generations") or {}
            cursor_projection = _read_json(self.cursors_path)["seats"]
            changed_seats = [
                seat["id"]
                for seat in active_agents
                if seat["id"] in cursor_projection
                and cursor_projection[seat["id"]].get("roster_generation")
                != armed_generations.get(seat["id"])
            ]
            if changed_seats:
                messages = self._messages()
                baseline = messages[-1]["id"] if messages else None
                cursors = _read_json(self.cursors_path)
                for seat_id in changed_seats:
                    cursor = cursors["seats"].get(seat_id)
                    if cursor is None:
                        continue
                    cursor["last_verified_message_id"] = baseline
                    cursor["roster_generation"] = armed_generations.get(seat_id)
                    cursor["last_outcome"] = "rearmed_baseline"
                    cursor["updated_at"] = _iso(now)
                    self._append_event(
                        "seat_rearmed_baseline",
                        seat_id,
                        {
                            "window_id": window_id,
                            "cursor_message_id": baseline,
                            "historical_backlog_inherited": False,
                            "previous_generation": cursor_projection[seat_id].get(
                                "roster_generation"
                            ),
                            "current_generation": armed_generations.get(seat_id),
                        },
                    )
                _atomic_json(self.cursors_path, cursors)
            self._append_event(
                "window_armed",
                "user",
                {
                    "window_id": window_id,
                    "armed_by": "user",
                    "deadline": _iso(parsed_deadline),
                    "max_quiet_cycles": quiet_cycles,
                    "max_wakes": max_wakes,
                    "default_was_closed": True,
                    "armed_seat_generations": armed_generations,
                    "policy_revision": POLICY_REVISION,
                    "policy_sha256": _sha256(policy),
                },
            )
        return self.snapshot()["wake_window"]

    def drain_window(self, *, actor: str = "user", reason: str = "user_requested") -> dict[str, Any]:
        if actor != "user":
            raise GateBlocked("only the user may request draining", code="user_action_required")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 128:
            raise WakeupError("drain reason must be a short nonempty string", code="invalid_input")
        with self._locked():
            self._verify_binding()
            self._assert_no_incomplete_completion()
            events = self._verify_chain(self.events_path, "event_hash")
            window = self._window(events, self._intent_states())
            if window["state"] != "active":
                raise GateBlocked("only an active wake window can begin draining", code="window_not_active")
            self._append_event(
                "window_draining",
                "user",
                {"window_id": window["window_id"], "reason": reason.strip()},
            )
        return self.snapshot()["wake_window"]

    def _close_receipt(self, window: Mapping[str, Any], event_head: str | None) -> dict[str, Any]:
        binding = _read_json(self.binding_path)
        receipt = {
            "schema_version": CLOSE_RECEIPT_SCHEMA,
            "receipt_id": "wake-close-" + uuid.uuid4().hex,
            "project_id": binding["project_id"],
            "project_root": binding["project_root"],
            "room_identity": binding["room_identity"],
            "window_id": window["window_id"],
            "closed_at": _iso(self._now()),
            "closed_by": "user",
            "pending_intents": 0,
            "active_leases": 0,
            "event_head_before_close": event_head,
        }
        receipt["close_receipt_sha256"] = _sha256(receipt)
        return receipt

    def close_window(self, *, actor: str = "user") -> dict[str, Any]:
        if actor != "user":
            raise GateBlocked("only the user may close a wake window", code="user_action_required")
        with self._locked():
            self._verify_binding()
            self._assert_no_incomplete_completion()
            events = self._verify_chain(self.events_path, "event_hash")
            window = self._window(events, self._intent_states())
            if window["state"] != "draining":
                raise GateBlocked("wake window must be draining before close", code="window_not_draining")
            if window["pending_intents"] or window["active_leases"]:
                raise GateBlocked("pending intent or active lease prevents close", code="window_not_drained")
            head = events[-1]["event_hash"] if events else None
            receipt = self._close_receipt(window, head)
            self._append_event(
                "window_closed",
                "user",
                {
                    "window_id": window["window_id"],
                    "close_receipt_sha256": receipt["close_receipt_sha256"],
                    "pending_intents": 0,
                    "active_leases": 0,
                },
            )
            return receipt

    def _expire(self, intents: Mapping[str, Mapping[str, Any]], window_id: str | None) -> list[str]:
        now = self._now()
        expired: list[str] = []
        for intent_id, intent in sorted(intents.items()):
            if intent.get("window_id") != window_id or intent.get("status") not in {"pending", "claimed"}:
                continue
            if _parse_time(intent["expires_at"], "expires_at") > now:
                continue
            payload = {
                "window_id": window_id,
                "outcome": "root_hold",
                "hold_reason": "intent_timeout",
                "escalation_seat_id": "user",
                "cursor_advanced": False,
                "awaiting_user_disposition": True,
            }
            self._append_intent("timed_out", intent, payload)
            self._fault_hook("after_timed_out_intent")
            self._append_event(
                "intent_timed_out",
                intent["seat_id"],
                {"window_id": window_id, "intent_id": intent_id, **payload},
            )
            expired.append(intent_id)
        return expired

    def dispose_hold(
        self,
        *,
        intent_id: str,
        source_message_id: str,
        window_id: str,
        reason: str,
        action: str = "retry_once",
        actor: str = "user",
    ) -> dict[str, Any]:
        """Record an exact, user-only decision without claiming Agent success.

        V5 intentionally implements only ``retry_once``.  It leaves both
        cursors untouched and authorizes one later intent for the same source.
        """

        if actor != "user":
            raise GateBlocked("only the user may dispose a wake hold", code="user_action_required")
        _opaque(intent_id, "intent_id")
        _opaque(source_message_id, "source_message_id")
        _opaque(window_id, "window_id")
        if action != "retry_once":
            raise WakeupError("V5 hold disposition supports only retry_once", code="invalid_input")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256 or "\n" in reason:
            raise WakeupError("hold disposition reason must be one short line", code="invalid_input")
        with self._locked():
            self._verify_binding()
            self._policy()
            self._assert_no_incomplete_completion()
            self._repair_transition_event_splits()
            states = self._intent_states()
            intent = states.get(intent_id)
            if intent is not None and intent.get("status") == "user_retry_authorized":
                if (
                    intent.get("source_message_id") != source_message_id
                    or intent.get("window_id") != window_id
                    or intent.get("disposition") != action
                    or intent.get("reason") != reason.strip()
                ):
                    raise ProjectBindingError(
                        "hold disposition replay differs from the recorded decision"
                    )
                return {
                    key: copy.deepcopy(intent[key])
                    for key in (
                        "window_id",
                        "source_message_id",
                        "disposition_id",
                        "disposition",
                        "reason",
                        "decided_by",
                        "decided_at",
                        "cursor_advanced",
                        "agent_success_claimed",
                    )
                } | {"idempotent_replay": True}
            if intent is None or intent.get("status") not in {
                "root_hold",
                "terminal_hold",
                "dual_consumer_hold",
            }:
                raise GateBlocked("intent is not awaiting user disposition", code="hold_unavailable")
            if (
                intent.get("source_message_id") != source_message_id
                or intent.get("window_id") != window_id
            ):
                raise ProjectBindingError("hold disposition differs from source or window binding")
            payload = {
                "window_id": window_id,
                "source_message_id": source_message_id,
                "disposition_id": "wake-disposition-" + uuid.uuid4().hex,
                "disposition": action,
                "reason": reason.strip(),
                "decided_by": "user",
                "decided_at": _iso(self._now()),
                "cursor_advanced": False,
                "agent_success_claimed": False,
            }
            self._append_intent("user_retry_authorized", intent, payload)
            self._fault_hook("after_disposition_intent")
            self._append_event(
                "wake_hold_disposition",
                "user",
                {"intent_id": intent_id, "seat_id": intent["seat_id"], **payload},
            )
            return copy.deepcopy(payload)

    def _transition_for_limits(self, window: Mapping[str, Any], reason: str) -> None:
        if window["state"] == "active":
            self._append_event(
                "window_draining",
                "user",
                {"window_id": window["window_id"], "reason": reason},
            )

    @staticmethod
    def _relevant(message: Mapping[str, Any], seat: Mapping[str, Any]) -> bool:
        seat_id = seat["id"]
        if message.get("speaker") == seat_id:
            return False
        audience = message.get("audience")
        return audience == seat_id or (
            audience == "all" and seat.get("receives_broadcast") is True
        )

    @staticmethod
    def _after_cursor(messages: Sequence[Mapping[str, Any]], cursor: str | None) -> list[dict[str, Any]]:
        if cursor is None:
            return [copy.deepcopy(dict(item)) for item in messages]
        for index, message in enumerate(messages):
            if message.get("id") == cursor:
                return [copy.deepcopy(dict(item)) for item in messages[index + 1 :]]
        raise WakeupError("wakeup cursor points to a missing message", code="cursor_invalid")

    def discover(self, seat_id: str | None = None) -> dict[str, Any]:
        if seat_id is not None:
            _opaque(seat_id, "seat_id")
        with self._locked():
            binding = self._verify_binding()
            self._assert_no_incomplete_completion()
            policy = self._policy()
            repaired_transitions = self._repair_transition_event_splits()
            events = self._verify_chain(self.events_path, "event_hash")
            intents = self._intent_states()
            messages = self._messages()
            roster = _read_json(self.roster_path)
            baselined = self._sync_seats(roster=roster, messages=messages)
            window = self._window(self._verify_chain(self.events_path, "event_hash"), intents)
            expired = self._expire(intents, window["window_id"])
            if expired:
                intents = self._intent_states()
                events = self._verify_chain(self.events_path, "event_hash")
                window = self._window(events, intents)
                # A timeout escalation is the sole result of this discovery cycle.
                # Reissuing the same source immediately would defeat root escalation.
                return {
                    "intent": None,
                    "reason": "timeout_escalated_to_root",
                    "baselined_seats": baselined,
                    "escalated_intent_ids": expired,
                    "silent": False,
                }

            if window["state"] == "active" and self._now() >= _parse_time(window["deadline"], "deadline"):
                self._transition_for_limits(window, "deadline_reached")
                window["state"] = "draining"
            if window["state"] == "active" and window["wakes"] >= window["max_wakes"]:
                self._transition_for_limits(window, "max_wakes_reached")
                window["state"] = "draining"

            # A durable pending intent is safe to replay: it contains no raw
            # message or host secret, and claim still requires the exact HMAC
            # proof.  This covers both a repaired intent/event split and the
            # case where both ledgers committed but the original response was
            # lost.  Claimed intents are deliberately not returned here.
            pending_replays = [
                value
                for value in intents.values()
                if value.get("status") == "pending"
                and (seat_id is None or value.get("seat_id") == seat_id)
            ]
            if pending_replays:
                pending_intent = pending_replays[0]
                self._revalidate_intent_binding(pending_intent, intents)
                was_repaired = (
                    pending_intent["intent_id"],
                    "created",
                ) in repaired_transitions
                return {
                    "intent": copy.deepcopy(pending_intent),
                    "reason": (
                        "intent_recovered"
                        if was_repaired
                        else "open_intent_replayed"
                    ),
                    "baselined_seats": baselined,
                    "escalated_intent_ids": expired,
                    "silent": False,
                    "idempotent_replay": True,
                }
            if window["state"] != "active":
                return {
                    "intent": None,
                    "reason": "window_" + window["state"],
                    "baselined_seats": baselined,
                    "escalated_intent_ids": expired,
                    "silent": True,
                }

            cursors = _read_json(self.cursors_path)
            open_by_seat = {
                value["seat_id"]
                for value in intents.values()
                if value.get("status") in {"pending", "claimed"}
            }
            creation_records = [
                record
                for record in self._verify_chain(self.intents_path, "record_hash")
                if record.get("action") == "created"
            ]
            now = self._now()
            blockers: list[str] = []
            had_relevant = False
            agents = [item for item in self._roster_agents(roster) if item.get("active") is True]
            seat_generations = self._seat_generations()
            if seat_id is not None:
                agents = [item for item in agents if item.get("id") == seat_id]
                if not agents:
                    raise GateBlocked("requested wake seat is not an active agent", code="seat_unavailable")

            for seat in agents:
                current_seat = seat["id"]
                seat_policy = self._seat_policy(policy, current_seat)
                if not seat_policy["enabled"] or seat_policy["mode"] != "intent_only":
                    continue
                current_generation = seat_generations.get(current_seat)
                if (
                    not isinstance(current_generation, str)
                    or window["armed_seat_generations"].get(current_seat) != current_generation
                ):
                    blockers.append("requires_rearm")
                    had_relevant = True
                    continue
                if current_seat in open_by_seat:
                    blockers.append("open_intent")
                    # An outstanding intent/lease is work in progress, never a
                    # quiet observation cycle.  In particular it must not
                    # consume max_quiet_cycles or force the window to drain.
                    had_relevant = True
                    continue
                cursor = cursors["seats"][current_seat]["last_verified_message_id"]
                pending = [
                    item
                    for item in self._after_cursor(messages, cursor)
                    if self._relevant(item, seat)
                ]
                if not pending:
                    continue
                had_relevant = True
                adapter = self._adapter(policy, seat_policy["adapter_id"])
                message = pending[0]
                message_id = message["id"]
                idempotency_key = _sha256(
                    {
                        "project_id": binding["project_id"],
                        "room_identity": binding["room_identity"],
                        "seat_id": current_seat,
                        "source_message_id": message_id,
                    }
                )
                attempt_history = [
                    value
                    for value in intents.values()
                    if value.get("idempotency_key") == idempotency_key
                ]
                held = next(
                    (
                        value
                        for value in reversed(attempt_history)
                        if value.get("status") in {
                            "root_hold",
                            "terminal_hold",
                            "dual_consumer_hold",
                        }
                    ),
                    None,
                )
                if held is not None:
                    blockers.append(str(held["status"]))
                    continue
                retry_authorized = bool(
                    attempt_history
                    and attempt_history[-1].get("status") == "user_retry_authorized"
                )
                if any(value.get("status") == "sent_and_verified" for value in attempt_history):
                    raise ChainError("verified source remains behind its wake cursor")
                failed_attempts = [value for value in attempt_history if value.get("status") == "failed"]
                if (
                    len(attempt_history) >= policy["retry_policy"]["max_attempts"]
                    and not retry_authorized
                ):
                    blockers.append("terminal_hold")
                    continue
                if failed_attempts and not retry_authorized:
                    last_failure = max(
                        _parse_time(value["completed_at"], "completed_at")
                        for value in failed_attempts
                    )
                    retry_delay = policy["retry_policy"]["backoff_seconds"][
                        min(len(failed_attempts) - 1, 2)
                    ]
                    if now - last_failure < timedelta(seconds=retry_delay):
                        blockers.append(f"retry_backoff_{retry_delay}s")
                        continue
                recent = [
                    record
                    for record in creation_records
                    if record["seat_id"] == current_seat
                    and now - _parse_time(record["timestamp"], "intent timestamp") < timedelta(hours=1)
                ]
                if len(recent) >= seat_policy["hourly_limit"]:
                    blockers.append("hourly_quota")
                    continue
                if recent:
                    last = max(_parse_time(record["timestamp"], "intent timestamp") for record in recent)
                    if now - last < timedelta(seconds=seat_policy["cooldown_seconds"]):
                        blockers.append("cooldown")
                        continue

                intent_id = "wake-intent-" + uuid.uuid4().hex
                trigger_fingerprint = _sha256(
                    {
                        "message_id": message_id,
                        "speaker": message.get("speaker"),
                        "audience": message.get("audience"),
                        "timestamp": message.get("timestamp"),
                    }
                )
                intent = {
                    "intent_id": intent_id,
                    "window_id": window["window_id"],
                    "project_id": binding["project_id"],
                    "project_root": binding["project_root"],
                    "room_identity": binding["room_identity"],
                    "policy_revision": POLICY_REVISION,
                    "policy_sha256": binding["policy_sha256"],
                    "seat_id": current_seat,
                    "seat_generation": current_generation,
                    "source_message_id": message_id,
                    "trigger_fingerprint": trigger_fingerprint,
                    "idempotency_key": idempotency_key,
                    "attempt": len(attempt_history) + 1,
                    "adapter_id": adapter["adapter_id"],
                    "adapter_sha256": adapter["adapter_sha256"],
                    "key_id": binding["receipt_key_id"],
                    "created_at": _iso(now),
                    "expires_at": _iso(now + timedelta(seconds=seat_policy["timeout_seconds"])),
                    "max_messages": 1,
                    "raw_message_stored": False,
                    "direct_launch_performed": False,
                    "external_network_performed": False,
                }
                if window["quiet_cycles"]:
                    self._append_event(
                        "quiet_cycle_reset",
                        current_seat,
                        {"window_id": window["window_id"]},
                    )
                self._append_intent("created", intent, intent)
                self._fault_hook("after_created_intent")
                self._append_event(
                    "wake_intent_created",
                    current_seat,
                    {
                        "window_id": window["window_id"],
                        "intent_id": intent_id,
                        "source_message_id": message_id,
                        "idempotency_key": idempotency_key,
                        "attempt": len(attempt_history) + 1,
                        "max_messages": 1,
                    },
                )
                self._fault_hook("after_created_event")
                if window["wakes"] + 1 >= window["max_wakes"]:
                    self._transition_for_limits({**window, "wakes": window["wakes"] + 1}, "max_wakes_reached")
                return {
                    "intent": intent,
                    "reason": "intent_created",
                    "baselined_seats": baselined,
                    "escalated_intent_ids": expired,
                    "silent": False,
                }

            if had_relevant:
                return {
                    "intent": None,
                    "reason": "gated:" + ",".join(sorted(set(blockers))),
                    "baselined_seats": baselined,
                    "escalated_intent_ids": expired,
                    "silent": True,
                }

            self._append_event(
                "quiet_cycle",
                "user",
                {"window_id": window["window_id"], "external_notification": False},
            )
            quiet = window["quiet_cycles"] + 1
            if quiet >= window["max_quiet_cycles"]:
                self._transition_for_limits({**window, "quiet_cycles": quiet}, "quiet_cycles_reached")
            return {
                "intent": None,
                "reason": "idle_silent",
                "baselined_seats": baselined,
                "escalated_intent_ids": expired,
                "silent": True,
            }

    def claim(
        self,
        *,
        intent_id: str,
        adapter_id: str,
        adapter_sha256: str,
        claim_proof_hmac: str | None = None,
    ) -> dict[str, Any]:
        _opaque(intent_id, "intent_id")
        _opaque(adapter_id, "adapter_id")
        _digest(adapter_sha256, "adapter_sha256")
        if not isinstance(claim_proof_hmac, str) or not HMAC_RE.fullmatch(claim_proof_hmac):
            raise GateBlocked("claim requires a valid HMAC proof", code="claim_proof_invalid")
        with self._locked():
            self._verify_binding()
            self._assert_no_incomplete_completion()
            policy = self._policy()
            self._repair_transition_event_splits()
            states = self._intent_states()
            intent = states.get(intent_id)
            if intent is None or intent.get("status") not in {"pending", "claimed"}:
                raise GateBlocked("intent is unavailable for claim", code="intent_unavailable")
            self._revalidate_intent_binding(intent, states)
            adapter = self._adapter(policy, adapter_id)
            if adapter["adapter_sha256"] != adapter_sha256:
                raise GateBlocked("adapter fingerprint differs from registration", code="adapter_unauthorized")
            if intent["adapter_id"] != adapter_id or intent["adapter_sha256"] != adapter_sha256:
                raise GateBlocked("adapter does not own this intent", code="adapter_unauthorized")
            if intent["seat_id"] not in adapter["allowed_seat_ids"]:
                raise GateBlocked("adapter registration does not own this seat", code="adapter_unauthorized")
            if self._now() >= _parse_time(intent["expires_at"], "expires_at"):
                raise GateBlocked("intent lease has expired", code="intent_expired")
            receipt_key, key_binding = self._receipt_key()
            if intent.get("key_id") != key_binding["receipt_key_id"]:
                raise GateBlocked("intent key id differs from project key", code="claim_proof_invalid")
            expected_proof = build_claim_proof(intent, receipt_key)
            if not hmac.compare_digest(claim_proof_hmac, expected_proof):
                raise GateBlocked("claim HMAC proof does not verify", code="claim_proof_invalid")
            if intent.get("status") == "claimed":
                return {**copy.deepcopy(intent), "status": "claimed", "idempotent_replay": True}
            claim_id = "wake-claim-" + uuid.uuid4().hex
            nonce = secrets.token_hex(32)
            payload = {
                "window_id": intent["window_id"],
                "claim_id": claim_id,
                "nonce": nonce,
                "key_id": key_binding["receipt_key_id"],
                "claimed_at": _iso(self._now()),
                "adapter_id": adapter_id,
                "adapter_sha256": adapter_sha256,
            }
            self._append_intent("claimed", intent, payload)
            self._fault_hook("after_claimed_intent")
            self._append_event(
                "wake_intent_claimed",
                intent["seat_id"],
                {"window_id": intent["window_id"], "intent_id": intent_id, **payload},
            )
            return {**intent, **payload, "status": "claimed"}

    @staticmethod
    def _validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
        _reject_dangerous_fields(receipt)
        expected = {
            "schema_version",
            "receipt_id",
            "project_id",
            "project_root",
            "room_identity",
            "intent_id",
            "window_id",
            "policy_revision",
            "policy_sha256",
            "claim_id",
            "nonce",
            "key_id",
            "seat_id",
            "seat_generation",
            "source_message_id",
            "idempotency_key",
            "adapter_id",
            "adapter_sha256",
            "status",
            "sent_at",
            "verified_at",
            "delivery_ref",
            "failure_code",
            "external_command_accepted",
            "secrets_stored",
            "receipt_sha256",
            "receipt_hmac",
        }
        _strict_keys(receipt, expected, "wakeup receipt")
        if receipt.get("schema_version") != RECEIPT_SCHEMA:
            raise WakeupError("unsupported wakeup receipt schema", code="receipt_invalid")
        for key in (
            "receipt_id",
            "project_id",
            "intent_id",
            "window_id",
            "claim_id",
            "key_id",
            "seat_id",
            "source_message_id",
            "adapter_id",
        ):
            _opaque(receipt.get(key), key)
        if receipt.get("policy_revision") != POLICY_REVISION:
            raise WakeupError("receipt policy revision is invalid", code="receipt_invalid")
        for key in (
            "room_identity",
            "policy_sha256",
            "seat_generation",
            "idempotency_key",
            "adapter_sha256",
            "receipt_sha256",
        ):
            _digest(receipt.get(key), key)
        if not isinstance(receipt.get("project_root"), str) or not receipt["project_root"]:
            raise WakeupError("receipt project_root is invalid", code="receipt_invalid")
        if not isinstance(receipt.get("nonce"), str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["nonce"]):
            raise WakeupError("receipt nonce is invalid", code="receipt_invalid")
        if not isinstance(receipt.get("receipt_hmac"), str) or not HMAC_RE.fullmatch(receipt["receipt_hmac"]):
            raise WakeupError("receipt HMAC is invalid", code="receipt_invalid")
        if receipt.get("external_command_accepted") is not False or receipt.get("secrets_stored") is not False:
            raise WakeupError("receipt violates command/secret boundary", code="receipt_invalid")
        status = receipt.get("status")
        if status == "sent_and_verified":
            _parse_time(receipt.get("sent_at"), "sent_at")
            _parse_time(receipt.get("verified_at"), "verified_at")
            delivery = receipt.get("delivery_ref")
            if not isinstance(delivery, str) or not delivery or len(delivery) > 256 or "\n" in delivery:
                raise WakeupError("verified receipt requires delivery_ref", code="receipt_invalid")
            if receipt.get("failure_code") is not None:
                raise WakeupError("verified receipt cannot include failure_code", code="receipt_invalid")
        elif status == "failed":
            failure = receipt.get("failure_code")
            if not isinstance(failure, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", failure):
                raise WakeupError("failed receipt requires canonical failure_code", code="receipt_invalid")
            if any(receipt.get(key) is not None for key in ("sent_at", "verified_at", "delivery_ref")):
                raise WakeupError("failed receipt cannot claim delivery timestamps", code="receipt_invalid")
        else:
            raise WakeupError("receipt status must be sent_and_verified or failed", code="receipt_invalid")
        if receipt["receipt_sha256"] != receipt_sha256(receipt):
            raise WakeupError("receipt sha256 mismatch", code="receipt_invalid")
        return copy.deepcopy(dict(receipt))

    def _prepare_completion(
        self,
        intent: Mapping[str, Any],
        receipt: Mapping[str, Any],
        *,
        completed_at: str,
    ) -> dict[str, Any]:
        journal = self._load_completion_journal()
        intent_id = intent["intent_id"]
        existing = journal["entries"].get(intent_id)
        if existing is not None:
            if _canonical(existing.get("receipt")) != _canonical(receipt):
                raise GateBlocked(
                    "completion journal is bound to a different receipt",
                    code="completion_receipt_conflict",
                )
            return copy.deepcopy(existing)
        if any(entry.get("state") != "committed" for entry in journal["entries"].values()):
            raise GateBlocked(
                "another interrupted completion requires replay",
                code="completion_recovery_required",
            )
        wake_cursor = _read_json(self.cursors_path)["seats"].get(intent["seat_id"])
        if not isinstance(wake_cursor, dict):
            raise ChainError("wake cursor is missing during completion prepare")
        panel_cursor = _read_json(self._panel_cursor_path(intent["seat_id"]))
        entry = {
            "intent_id": intent_id,
            "window_id": intent["window_id"],
            "seat_id": intent["seat_id"],
            "seat_generation": intent["seat_generation"],
            "policy_revision": intent["policy_revision"],
            "policy_sha256": intent["policy_sha256"],
            "claim_id": intent["claim_id"],
            "source_message_id": intent["source_message_id"],
            "outcome": receipt["status"],
            "receipt": copy.deepcopy(dict(receipt)),
            "receipt_sha256": receipt["receipt_sha256"],
            "completed_at": completed_at,
            "wake_cursor_before": wake_cursor.get("last_verified_message_id"),
            "panel_cursor_before": panel_cursor.get("last_seen_id"),
            "state": "prepared",
            "prepared_at": _iso(self._now()),
            "committed_at": None,
        }
        journal["entries"][intent_id] = entry
        self._write_completion_journal(journal)
        self._fault_hook("after_journal_prepare")
        return copy.deepcopy(entry)

    def _completion_cursor_precondition(self, intent: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """Require both projections to name the exact same source predecessor."""

        wake_cursor = _read_json(self.cursors_path)["seats"].get(intent["seat_id"])
        if not isinstance(wake_cursor, dict):
            raise ChainError("wake cursor is missing during completion")
        panel_cursor = _read_json(self._panel_cursor_path(intent["seat_id"]))
        wake_before = wake_cursor.get("last_verified_message_id")
        panel_before = panel_cursor.get("last_seen_id")
        if wake_before != panel_before:
            raise GateBlocked(
                "wake and panel cursors do not share the source predecessor",
                code="dual_consumer_conflict",
            )
        roster = _read_json(self.roster_path)
        seat = next(
            item for item in self._roster_agents(roster) if item.get("id") == intent["seat_id"]
        )
        pending = [
            item
            for item in self._after_cursor(self._messages(), wake_before)
            if self._relevant(item, seat)
        ]
        if not pending or pending[0].get("id") != intent["source_message_id"]:
            raise GateBlocked(
                "wake source is not the next message after the shared predecessor",
                code="dual_consumer_conflict",
            )
        return wake_before, panel_before

    def _record_dual_consumer_hold(
        self,
        intent: Mapping[str, Any],
        receipt: Mapping[str, Any],
        *,
        reason: str,
    ) -> None:
        payload = {
            "window_id": intent["window_id"],
            "source_message_id": intent["source_message_id"],
            "outcome": "dual_consumer_hold",
            "hold_reason": reason,
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": receipt["receipt_sha256"],
            "cursor_advanced": False,
            "awaiting_user_disposition": True,
        }
        self._append_intent("dual_consumer_hold", intent, payload)
        self._fault_hook("after_dual_consumer_hold_intent")
        self._append_event(
            "wake_dual_consumer_hold",
            intent["seat_id"],
            {"intent_id": intent["intent_id"], **payload},
        )

    def _assert_recoverable_cursor(
        self,
        *,
        current_id: str | None,
        before_id: str | None,
        source_id: str,
        label: str,
    ) -> None:
        if current_id not in {before_id, source_id}:
            raise GateBlocked(
                f"{label} cursor moved outside the prepared completion",
                code="dual_consumer_conflict",
            )

    def _verify_committed_completion(
        self,
        intent: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt = entry["receipt"]
        records = self._verify_chain(self.intents_path, "record_hash")
        completed = [
            record
            for record in records
            if record.get("intent_id") == intent["intent_id"]
            and record.get("action") == "completed"
            and record.get("payload", {}).get("receipt_sha256") == receipt["receipt_sha256"]
        ]
        if len(completed) != 1:
            raise ChainError("committed completion lacks one exact intent record")
        events = self._verify_chain(self.events_path, "event_hash")
        matching_events = [
            event
            for event in events
            if event.get("action") == "wake_intent_completed"
            and event.get("payload", {}).get("intent_id") == intent["intent_id"]
            and event.get("payload", {}).get("receipt_sha256") == receipt["receipt_sha256"]
        ]
        if len(matching_events) != 1:
            raise ChainError("committed completion lacks one exact event record")
        if entry["outcome"] == "sent_and_verified":
            wake_cursor = _read_json(self.cursors_path)["seats"][intent["seat_id"]]
            panel_cursor = _read_json(self._panel_cursor_path(intent["seat_id"]))
            source_position = self._message_position(intent["source_message_id"])
            if (
                self._message_position(wake_cursor.get("last_verified_message_id")) < source_position
                or self._message_position(panel_cursor.get("last_seen_id")) < source_position
            ):
                raise ChainError("committed completion cursor projection is incomplete")
        return {
            "intent_id": intent["intent_id"],
            "outcome": entry["outcome"],
            "receipt_outcome": entry["outcome"],
            "cursor_advanced": entry["outcome"] == "sent_and_verified",
            "terminal_hold": False,
            "idempotent_replay": True,
        }

    def _recover_completion(
        self,
        intent: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        if entry.get("state") == "committed":
            return self._verify_committed_completion(intent, entry)

        outcome = entry["outcome"]
        receipt = entry["receipt"]
        if outcome == "sent_and_verified":
            wake_current = _read_json(self.cursors_path)["seats"][intent["seat_id"]].get(
                "last_verified_message_id"
            )
            panel_current = _read_json(self._panel_cursor_path(intent["seat_id"])).get(
                "last_seen_id"
            )
            self._assert_recoverable_cursor(
                current_id=wake_current,
                before_id=entry.get("wake_cursor_before"),
                source_id=intent["source_message_id"],
                label="wake",
            )
            self._assert_recoverable_cursor(
                current_id=panel_current,
                before_id=entry.get("panel_cursor_before"),
                source_id=intent["source_message_id"],
                label="panel",
            )
        states = self._intent_states()
        state = states.get(intent["intent_id"])
        if state is None:
            raise ChainError("completion journal intent is missing")
        payload = {
            "window_id": intent["window_id"],
            "outcome": outcome,
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": receipt["receipt_sha256"],
            "completed_at": entry["completed_at"],
            "cursor_advanced": outcome == "sent_and_verified",
        }
        if state.get("status") == "claimed":
            self._append_intent("completed", state, payload)
        elif state.get("status") not in {outcome, "terminal_hold"}:
            raise ChainError("completion journal conflicts with intent state")
        elif state.get("receipt_sha256") != receipt["receipt_sha256"]:
            raise ChainError("completion journal receipt differs from intent state")
        self._fault_hook("after_intent_append")

        events = self._verify_chain(self.events_path, "event_hash")
        matching_events = [
            event
            for event in events
            if event.get("action") == "wake_intent_completed"
            and event.get("payload", {}).get("intent_id") == intent["intent_id"]
        ]
        if len(matching_events) > 1:
            raise ChainError("completion event was appended more than once")
        if matching_events:
            if matching_events[0].get("payload", {}).get("receipt_sha256") != receipt["receipt_sha256"]:
                raise ChainError("completion event receipt differs from journal")
        else:
            self._append_event(
                "wake_intent_completed",
                intent["seat_id"],
                {"window_id": intent["window_id"], "intent_id": intent["intent_id"], **payload},
            )
        self._fault_hook("after_event_append")

        if outcome == "sent_and_verified":
            wake_cursors = _read_json(self.cursors_path)
            wake_cursor = wake_cursors["seats"].get(intent["seat_id"])
            if not isinstance(wake_cursor, dict):
                raise ChainError("wake cursor vanished during completion recovery")
            if wake_cursor.get("last_verified_message_id") == entry.get("wake_cursor_before"):
                wake_cursor["last_verified_message_id"] = intent["source_message_id"]
                wake_cursor["last_outcome"] = "sent_and_verified"
                wake_cursor["updated_at"] = entry["completed_at"]
                _atomic_json(self.cursors_path, wake_cursors)
            self._fault_hook("after_wakeup_cursor")

            panel_cursor_path = self._panel_cursor_path(intent["seat_id"])
            panel_cursor = _read_json(panel_cursor_path)
            if panel_cursor.get("last_seen_id") == entry.get("panel_cursor_before"):
                panel_cursor["last_seen_id"] = intent["source_message_id"]
                panel_cursor["updated_at"] = entry["completed_at"]
                _atomic_json(panel_cursor_path, panel_cursor)
            self._fault_hook("after_panel_cursor")

        journal = self._load_completion_journal()
        current_entry = journal["entries"].get(intent["intent_id"])
        if not isinstance(current_entry, dict):
            raise ChainError("completion journal entry vanished")
        current_entry["state"] = "committed"
        current_entry["committed_at"] = _iso(self._now())
        self._write_completion_journal(journal)
        self._fault_hook("after_journal_commit")
        return {
            "intent_id": intent["intent_id"],
            "outcome": outcome,
            "receipt_outcome": outcome,
            "cursor_advanced": outcome == "sent_and_verified",
            "terminal_hold": False,
            "idempotent_replay": state.get("status") != "claimed",
        }

    def _terminalize_failed_completion(
        self,
        intent: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = copy.deepcopy(dict(result))
        if result.get("receipt_outcome") != "failed":
            return value
        policy = self._policy()
        refreshed = self._intent_states()
        attempts = [
            item
            for item in refreshed.values()
            if item.get("idempotency_key") == intent["idempotency_key"]
        ]
        state = refreshed[intent["intent_id"]]
        if len(attempts) >= policy["retry_policy"]["max_attempts"]:
            if state.get("status") == "failed":
                terminal_payload = {
                    "window_id": intent["window_id"],
                    "source_message_id": intent["source_message_id"],
                    "outcome": "terminal_hold",
                    "hold_reason": "retry_attempts_exhausted",
                    "attempts": len(attempts),
                    "idempotency_key": intent["idempotency_key"],
                    "cursor_advanced": False,
                    "awaiting_user_disposition": True,
                }
                self._append_intent("terminal_hold", state, terminal_payload)
                self._fault_hook("after_terminal_hold_intent")
                self._append_event(
                    "wake_terminal_hold",
                    intent["seat_id"],
                    {"intent_id": intent["intent_id"], **terminal_payload},
                )
                state = self._intent_states()[intent["intent_id"]]
            if state.get("status") == "terminal_hold":
                value.update({"outcome": "terminal_hold", "terminal_hold": True})
        return value

    def complete(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._validate_receipt(receipt)
        with self._locked():
            binding = self._verify_binding()
            self._policy()
            self._repair_transition_event_splits()
            states = self._intent_states()
            intent = states.get(normalized["intent_id"])
            journal = self._load_completion_journal()
            entry = journal["entries"].get(normalized["intent_id"])
            if intent is None:
                raise GateBlocked("intent has no active claim", code="intent_unavailable")
            if entry is None and intent.get("status") == "dual_consumer_hold":
                raise GateBlocked("intent is held for dual-consumer conflict", code="dual_consumer_conflict")
            if entry is None and intent.get("status") != "claimed":
                raise GateBlocked("intent has no active claim", code="intent_unavailable")
            exact = {
                "project_id": binding["project_id"],
                "project_root": binding["project_root"],
                "room_identity": binding["room_identity"],
                "intent_id": intent["intent_id"],
                "window_id": intent["window_id"],
                "policy_revision": intent["policy_revision"],
                "policy_sha256": intent["policy_sha256"],
                "claim_id": intent["claim_id"],
                "nonce": intent["nonce"],
                "key_id": intent["key_id"],
                "seat_id": intent["seat_id"],
                "seat_generation": intent["seat_generation"],
                "source_message_id": intent["source_message_id"],
                "idempotency_key": intent["idempotency_key"],
                "adapter_id": intent["adapter_id"],
                "adapter_sha256": intent["adapter_sha256"],
            }
            if any(normalized.get(key) != value for key, value in exact.items()):
                raise ProjectBindingError("receipt differs from bound intent identity")
            receipt_key, key_binding = self._receipt_key()
            if normalized["key_id"] != key_binding["receipt_key_id"]:
                raise ProjectBindingError("receipt key id differs from project binding")
            expected_hmac = _receipt_hmac(normalized, receipt_key)
            if not hmac.compare_digest(normalized["receipt_hmac"], expected_hmac):
                raise WakeupError("receipt HMAC verification failed", code="receipt_hmac_invalid")
            if entry is not None:
                if _canonical(entry.get("receipt")) != _canonical(normalized):
                    raise GateBlocked(
                        "completion journal is bound to a different receipt",
                        code="completion_receipt_conflict",
                    )
                if entry.get("state") == "committed":
                    return self._terminalize_failed_completion(
                        intent,
                        self._recover_completion(intent, entry),
                    )
            self._revalidate_intent_binding(intent, states)
            now = self._now()
            expires_at = _parse_time(intent["expires_at"], "expires_at")
            if entry is None and now >= expires_at:
                self._expire({intent["intent_id"]: intent}, intent["window_id"])
                raise GateBlocked(
                    "expired receipt was escalated to root without cursor advance",
                    code="intent_expired",
                )
            if entry is None and normalized["status"] == "sent_and_verified":
                sent_at = _parse_time(normalized["sent_at"], "sent_at")
                verified_at = _parse_time(normalized["verified_at"], "verified_at")
                claimed_at = _parse_time(intent["claimed_at"], "claimed_at")
                if sent_at < claimed_at - timedelta(seconds=MAX_RECEIPT_CLOCK_SKEW_SECONDS):
                    raise WakeupError("receipt sent_at predates its claim", code="receipt_time_invalid")
                if sent_at > verified_at:
                    raise WakeupError("receipt sent_at is after verified_at", code="receipt_time_invalid")
                if verified_at > now + timedelta(seconds=MAX_RECEIPT_CLOCK_SKEW_SECONDS):
                    raise WakeupError("receipt verified_at is in the future", code="receipt_time_invalid")
                if verified_at > expires_at + timedelta(seconds=MAX_RECEIPT_CLOCK_SKEW_SECONDS):
                    raise WakeupError("receipt verification exceeds intent lease", code="receipt_time_invalid")
            if entry is None and normalized["status"] == "sent_and_verified":
                try:
                    self._completion_cursor_precondition(intent)
                except GateBlocked as exc:
                    if exc.code != "dual_consumer_conflict":
                        raise
                    self._record_dual_consumer_hold(intent, normalized, reason=str(exc))
                    raise
            entry = self._prepare_completion(
                intent,
                normalized,
                completed_at=_iso(now),
            )
            result = self._recover_completion(intent, entry)
            return self._terminalize_failed_completion(intent, result)

    def _validate_cursor_projection(self, events: Sequence[Mapping[str, Any]]) -> None:
        expected: dict[str, str | None] = {}
        expected_generation: dict[str, str | None] = {}
        for event in events:
            action = event.get("action")
            payload = event.get("payload") or {}
            if action == "seat_baselined":
                expected[event["seat_id"]] = payload.get("cursor_message_id")
                expected_generation[event["seat_id"]] = payload.get("seat_generation")
            elif action == "wake_intent_completed" and payload.get("outcome") == "sent_and_verified":
                intent_id = payload.get("intent_id")
                states = self._intent_states()
                expected[event["seat_id"]] = states[intent_id]["source_message_id"]
            elif action == "seat_rearmed_baseline":
                expected[event["seat_id"]] = payload.get("cursor_message_id")
                expected_generation[event["seat_id"]] = payload.get("current_generation")
        actual = _read_json(self.cursors_path)
        actual_seats = actual.get("seats")
        if not isinstance(actual_seats, dict) or set(actual_seats) != set(expected):
            raise ChainError("cursor projection seat set differs from event chain")
        for seat_id, message_id in expected.items():
            if actual_seats[seat_id].get("last_verified_message_id") != message_id:
                raise ChainError(f"cursor projection differs for {seat_id}")
            if actual_seats[seat_id].get("roster_generation") != expected_generation.get(seat_id):
                raise ChainError(f"cursor roster generation differs for {seat_id}")

    def validate(self) -> dict[str, Any]:
        with self._locked():
            binding = self._verify_binding()
            self._policy()
            events = self._verify_chain(self.events_path, "event_hash")
            intents = self._intent_states()
            self._assert_no_incomplete_completion()
            self._validate_transition_event_projections()
            self._validate_cursor_projection(events)
            journal = self._load_completion_journal()
            for intent_id, entry in journal["entries"].items():
                intent = intents.get(intent_id)
                if not isinstance(entry, dict) or intent is None:
                    raise ChainError("completion journal references a missing intent")
                self._verify_committed_completion(intent, entry)
            return {
                "ok": True,
                "project_id": binding["project_id"],
                "room_identity": binding["room_identity"],
                "event_count": len(events),
                "intent_count": len(intents),
                "event_head": events[-1]["event_hash"] if events else None,
            }

    def panel_snapshot(self, *, panel_lock_held: bool = False) -> dict[str, Any]:
        """Return a read-only projection with the declared cross-lock context."""

        return self.snapshot(panel_lock_held=panel_lock_held)

    def snapshot(self, *, panel_lock_held: bool = False) -> dict[str, Any]:
        """Return a strictly read-only projection, including unsynchronised seats."""

        guard = self._wakeup_locked() if panel_lock_held else self._locked()
        with guard:
            binding = self._verify_binding()
            policy = self._policy()
            self._messages()  # validate duplicate/message-log integrity without mutation
            roster = _read_json(self.roster_path)
            events = self._verify_chain(self.events_path, "event_hash")
            intents = self._intent_states()
            self._validate_transition_event_projections()
            incomplete = self._incomplete_completions()
            if not incomplete:
                self._validate_cursor_projection(events)
            cursors = _read_json(self.cursors_path)["seats"]
            window = self._window(events, intents)
            window["completion_recovery_required"] = bool(incomplete)
            seat_generations = self._seat_generations()
            now = self._now()
            creation_records = [
                record for record in self._verify_chain(self.intents_path, "record_hash") if record["action"] == "created"
            ]
            seat_rows = []
            for seat in self._roster_agents(roster):
                seat_id = seat["id"]
                cursor_projection = cursors.get(seat_id)
                seat_policy = self._seat_policy(policy, seat_id)
                if cursor_projection is None:
                    # A roster addition has no wake authority and inherits no history
                    # until discover performs the explicit first-seen baseline sync.
                    seat_policy = {
                        **copy.deepcopy(policy["default_policy"]),
                        "seat_id": seat_id,
                        "adapter_id": None,
                    }
                generation_matches = (
                    window["armed_seat_generations"].get(seat_id)
                    == seat_generations.get(seat_id)
                )
                effective_enabled = bool(
                    seat_policy["enabled"]
                    and seat.get("active") is True
                    and window["state"] == "active"
                    and generation_matches
                )
                requires_rearm = bool(
                    seat_policy["enabled"]
                    and seat.get("active") is True
                    and (window["state"] != "active" or not generation_matches)
                )
                open_intent = next(
                    (
                        value
                        for value in intents.values()
                        if value.get("seat_id") == seat_id and value.get("status") in {"pending", "claimed"}
                    ),
                    None,
                )
                incomplete_entry = next(
                    (
                        value
                        for value in incomplete.values()
                        if value.get("seat_id") == seat_id
                    ),
                    None,
                )
                recent = [
                    record
                    for record in creation_records
                    if record["seat_id"] == seat_id
                    and now - _parse_time(record["timestamp"], "intent timestamp") < timedelta(hours=1)
                ]
                latest = max(
                    (_parse_time(record["timestamp"], "intent timestamp") for record in recent),
                    default=None,
                )
                cooldown_until = (
                    _iso(latest + timedelta(seconds=seat_policy["cooldown_seconds"])) if latest else None
                )
                states_for_seat = [value for value in intents.values() if value.get("seat_id") == seat_id]
                held_intent = next(
                    (
                        value
                        for value in reversed(states_for_seat)
                        if value.get("status") in {
                            "root_hold",
                            "terminal_hold",
                            "dual_consumer_hold",
                        }
                    ),
                    None,
                )
                awaiting_disposition = held_intent is not None
                last_outcome = (
                    "completion_recovery_required"
                    if incomplete_entry is not None
                    else states_for_seat[-1]["status"]
                    if states_for_seat
                    else (
                        cursor_projection["last_outcome"]
                        if cursor_projection is not None
                        else "not_baselined"
                    )
                )
                seat_rows.append(
                    {
                        "seat_id": seat_id,
                        "active": seat.get("active") is True,
                        "enabled": seat_policy["enabled"],
                        "effective_enabled": bool(
                            effective_enabled
                            and not awaiting_disposition
                            and incomplete_entry is None
                        ),
                        "requires_rearm": requires_rearm,
                        "mode": seat_policy["mode"],
                        "adapter_id": seat_policy["adapter_id"],
                        "baseline_state": "baselined" if cursor_projection is not None else "not_baselined",
                        "cursor_message_id": (
                            cursor_projection["last_verified_message_id"]
                            if cursor_projection is not None
                            else None
                        ),
                        "pending_intent_id": (
                            incomplete_entry["intent_id"]
                            if incomplete_entry is not None
                            else open_intent["intent_id"] if open_intent else None
                        ),
                        "hold_intent_id": held_intent["intent_id"] if held_intent else None,
                        "awaiting_user_disposition": awaiting_disposition,
                        "completion_recovery_required": incomplete_entry is not None,
                        "cooldown_until": cooldown_until,
                        "hourly_used": len(recent),
                        "hourly_limit": seat_policy["hourly_limit"],
                        "last_outcome": last_outcome,
                    }
                )
            return {
                "schema_version": SNAPSHOT_SCHEMA,
                "project": {
                    "id": binding["project_id"],
                    "root": binding["project_root"],
                    "room_identity": binding["room_identity"],
                    "host": binding["host"],
                },
                "policy": {
                    "revision": binding["policy_revision"],
                    "sha256": binding["policy_sha256"],
                    "default_enabled": False,
                    "default_mode": "queue_only",
                    "requires_explicit_user_arm": True,
                    "registered_adapters": [
                        {
                            "adapter_id": item["adapter_id"],
                            "adapter_sha256": item["adapter_sha256"],
                            "kind": item["kind"],
                        }
                        for item in policy["registered_adapters"]
                    ],
                    "pending_host_installs": copy.deepcopy(policy["pending_host_installs"]),
                },
                "wake_window": window,
                "seats": seat_rows,
                "totals": {
                    "enabled": sum(row["enabled"] for row in seat_rows),
                    "disabled": sum(not row["enabled"] for row in seat_rows),
                    "open_intents": sum(row["pending_intent_id"] is not None for row in seat_rows),
                },
                "event_chain": {
                    "valid": True,
                    "count": len(events),
                    "head_hash": events[-1]["event_hash"] if events else None,
                },
                "boundary": {
                    "direct_process_launch": False,
                    "external_network": False,
                    "arbitrary_command_fields": False,
                    "launchd_or_cron_installation": "out_of_scope",
                    "advance_only_on": "sent_and_verified",
                    "max_messages_per_wake": 1,
                },
            }


def build_receipt(
    claim: Mapping[str, Any],
    *,
    receipt_key: bytes,
    status: str,
    delivery_ref: str | None = None,
    failure_code: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a strict host-adapter receipt; this helper performs no delivery."""

    timestamp = _iso(now or _utc_now())
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_id": "wake-receipt-" + uuid.uuid4().hex,
        "project_id": claim["project_id"],
        "project_root": claim["project_root"],
        "room_identity": claim["room_identity"],
        "intent_id": claim["intent_id"],
        "window_id": claim["window_id"],
        "policy_revision": claim["policy_revision"],
        "policy_sha256": claim["policy_sha256"],
        "claim_id": claim["claim_id"],
        "nonce": claim["nonce"],
        "key_id": claim["key_id"],
        "seat_id": claim["seat_id"],
        "seat_generation": claim["seat_generation"],
        "source_message_id": claim["source_message_id"],
        "idempotency_key": claim["idempotency_key"],
        "adapter_id": claim["adapter_id"],
        "adapter_sha256": claim["adapter_sha256"],
        "status": status,
        "sent_at": timestamp if status == "sent_and_verified" else None,
        "verified_at": timestamp if status == "sent_and_verified" else None,
        "delivery_ref": delivery_ref if status == "sent_and_verified" else None,
        "failure_code": failure_code if status == "failed" else None,
        "external_command_accepted": False,
        "secrets_stored": False,
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    receipt["receipt_hmac"] = _receipt_hmac(receipt, receipt_key)
    return receipt


def _default_policy_path() -> Path:
    return Path(__file__).resolve().parent.parent / "fixtures" / "collaboration-panel" / "wakeup-policies.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sublation V5 project-local wake-intent gate")
    parser.add_argument("--project-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize closed wakeup state")
    init.add_argument("--policy", default=str(_default_policy_path()))
    arm = sub.add_parser("arm", help="explicitly arm a bounded wake window as user")
    arm.add_argument("--deadline", required=True)
    arm.add_argument("--quiet-cycles", type=int, required=True)
    arm.add_argument("--max-wakes", type=int, required=True)
    sub.add_parser("drain", help="stop new intents and drain current leases")
    sub.add_parser("close", help="close a fully drained wake window")
    discover = sub.add_parser("discover", help="discover at most one wake intent")
    discover.add_argument("--seat")
    claim = sub.add_parser("claim", help="claim with an exact registered adapter identity")
    claim.add_argument("--intent-id", required=True)
    claim.add_argument("--adapter-id", required=True)
    claim.add_argument("--adapter-sha256", required=True)
    claim.add_argument("--claim-proof-hmac", required=True)
    complete = sub.add_parser("complete", help="verify a host-adapter receipt")
    complete.add_argument("--receipt", required=True)
    dispose = sub.add_parser("dispose-hold", help="record an exact user-only hold decision")
    dispose.add_argument("--intent-id", required=True)
    dispose.add_argument("--source-message-id", required=True)
    dispose.add_argument("--window-id", required=True)
    dispose.add_argument("--action", choices=("retry_once",), default="retry_once")
    dispose.add_argument("--reason", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("validate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = WakeupStore(args.project_root)
    try:
        if args.command == "init":
            result = store.initialize(args.policy)
        elif args.command == "arm":
            result = store.arm_window(
                deadline=args.deadline,
                quiet_cycles=args.quiet_cycles,
                max_wakes=args.max_wakes,
            )
        elif args.command == "drain":
            result = store.drain_window()
        elif args.command == "close":
            result = store.close_window()
        elif args.command == "discover":
            result = store.discover(args.seat)
        elif args.command == "claim":
            result = store.claim(
                intent_id=args.intent_id,
                adapter_id=args.adapter_id,
                adapter_sha256=args.adapter_sha256,
                claim_proof_hmac=args.claim_proof_hmac,
            )
        elif args.command == "complete":
            result = store.complete(_read_json(Path(args.receipt).expanduser().resolve()))
        elif args.command == "dispose-hold":
            result = store.dispose_hold(
                intent_id=args.intent_id,
                source_message_id=args.source_message_id,
                window_id=args.window_id,
                action=args.action,
                reason=args.reason,
            )
        elif args.command == "snapshot":
            result = store.snapshot()
        else:
            result = store.validate()
    except WakeupError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
