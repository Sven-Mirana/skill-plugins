#!/usr/bin/env python3
"""项目本地的 Sublation 多 Agent 协作面板运行时。

设计边界：

* 只监听 127.0.0.1，所有运行数据都在项目根目录下；
* 消息、任务、席位事件和路由展示回执均为 JSONL 追加写；
* 用户席固定存在，Agent 席默认上限 12，项目可显式设为 1--32；
* 路由接口只记录宿主状态，不导入、不调用也不执行任何 Skill。

本文件仅依赖 Python 标准库，可作为模块测试，也可直接作为 CLI 运行。
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


STATE_DIR_NAME = ".sublation-panel"
ROSTER_SCHEMA = "skill-sublation.collaboration-roster.v1"
ROSTER_EVENT_SCHEMA = "skill-sublation.collaboration-roster-event.v1"
PANEL_EVENT_SCHEMA = "skill-sublation.collaboration-panel-event.v1"
MESSAGE_SCHEMA = "skill-sublation.collaboration-message.v1"
TASK_EVENT_SCHEMA = "skill-sublation.collaboration-task-event.v1"
ROUTING_DISPLAY_SCHEMA = "skill-sublation.routing-display.v1"
CURSOR_SCHEMA = "skill-sublation.collaboration-cursor.v1"
CONFIG_SCHEMA = "skill-sublation.collaboration-panel-config.v1"

LOOPBACK_HOST = "127.0.0.1"
MIN_AGENT_SEATS = 1
DEFAULT_MAX_AGENT_SEATS = 12
MAX_AGENT_SEATS = 32
MAX_RETAINED_SEAT_RECORDS = 32  # 与候选 roster schema 的上限一致，包含墓碑。
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 20_000

SEAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RESERVED_AGENT_IDS = frozenset({"user", "root", "system", "all"})
OPEN_TASK_STATUSES = frozenset({"todo", "doing", "blocked"})
TASK_STATUSES = frozenset({"todo", "doing", "blocked", "done", "cancelled"})
ROUTING_STATES: dict[str, str] = {
    "suggested": "建议",
    "clarification_required": "待澄清",
    "invoked": "已调用",
    "completed": "已完成",
}
ROUTING_LAYERS: tuple[tuple[str, str], ...] = (
    ("metadata_classification", "分类画像"),
    ("advisory_recommendation", "建议路由"),
    ("host_selection", "宿主选择"),
    ("actual_invocation", "实际调用"),
)

FALLBACK_DEFAULT_AGENTS: tuple[dict[str, str], ...] = (
    {"id": "codex", "display_name": "Codex", "role_label": "实现与复核"},
    {"id": "hermes", "display_name": "Hermes", "role_label": "编排与观察"},
    {"id": "claude-code", "display_name": "Claude Code", "role_label": "独立复核"},
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_tag(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


class PanelError(Exception):
    """带有稳定错误码和 HTTP 状态的可展示错误。"""

    status = HTTPStatus.BAD_REQUEST
    code = "panel_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, "details": self.details}


class ValidationError(PanelError):
    code = "validation_error"


class NotFoundError(PanelError):
    status = HTTPStatus.NOT_FOUND
    code = "not_found"


class RevisionConflict(PanelError):
    status = HTTPStatus.CONFLICT
    code = "revision_conflict"


class SeatGuardError(PanelError):
    status = HTTPStatus.CONFLICT
    code = "seat_guard"


class WakeLeaseConflict(PanelError):
    status = HTTPStatus.CONFLICT
    code = "wake_lease_conflict"


class HostAdapterRequired(PanelError):
    status = HTTPStatus.NOT_IMPLEMENTED
    code = "requires_future_signed_host_adapter"


def _require_plain_text(value: Any, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field} 不能为空")
    if len(text) > max_length or "\n" in text or "\r" in text:
        raise ValidationError(f"{field} 必须是单行文本，且不超过 {max_length} 个字符")
    return text


def _validate_agent_id(value: Any) -> str:
    seat_id = _require_plain_text(value, "Agent ID", max_length=64).lower()
    if not SEAT_ID_RE.fullmatch(seat_id):
        raise ValidationError("Agent ID 只能使用小写字母、数字、点、下划线和连字符")
    if seat_id in RESERVED_AGENT_IDS:
        raise ValidationError(f"Agent ID {seat_id!r} 是保留标识")
    return seat_id


def _validate_opaque_id(value: Any, field: str) -> str:
    opaque_id = _require_plain_text(value, field, max_length=128)
    if not OPAQUE_ID_RE.fullmatch(opaque_id):
        raise ValidationError(f"{field} 只能使用字母、数字、点、下划线、冒号和连字符")
    return opaque_id


def _atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(dict(value)) + b"\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError("追加日志未完整写入")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise NotFoundError(f"缺少运行文件：{path.name}") from exc
    except json.JSONDecodeError as exc:
        raise PanelError(f"运行文件损坏：{path.name}", details={"line": exc.lineno}) from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PanelError(
                    f"追加日志损坏：{path.name}", details={"line": line_number}
                ) from exc
            if not isinstance(value, dict):
                raise PanelError(f"追加日志记录必须是对象：{path.name}", details={"line": line_number})
            records.append(value)
    return records


def _default_agent_specs() -> list[dict[str, str]]:
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "collaboration-panel" / "default-agents.json"
    if fixture.exists():
        try:
            payload = _read_json(fixture)
            agents = payload.get("agents", []) if isinstance(payload, dict) else []
            if agents:
                return [
                    {
                        "id": _validate_agent_id(item.get("id")),
                        "display_name": _require_plain_text(item.get("display_name"), "显示名称", max_length=64),
                        "role_label": _require_plain_text(item.get("role_label"), "角色说明", max_length=64),
                    }
                    for item in agents
                ]
        except PanelError:
            raise
        except (AttributeError, TypeError) as exc:
            raise PanelError("默认 Agent fixture 格式无效") from exc
    return [dict(item) for item in FALLBACK_DEFAULT_AGENTS]


def _normalize_agent_specs(specs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in specs:
        seat_id = _validate_agent_id(item.get("id"))
        if seat_id in seen:
            raise ValidationError(f"Agent ID 重复：{seat_id}")
        seen.add(seat_id)
        normalized.append(
            {
                "id": seat_id,
                "display_name": _require_plain_text(item.get("display_name"), "显示名称", max_length=64),
                "role_label": _require_plain_text(item.get("role_label", "协作 Agent"), "角色说明", max_length=64),
            }
        )
    return normalized


def _user_seat(timestamp: str, display_name: str) -> dict[str, Any]:
    return {
        "id": "user",
        "kind": "user",
        "display_name": display_name,
        "role_label": "决策与授权",
        "active": True,
        "visible": True,
        "receives_broadcast": True,
        "can_own_tasks": True,
        "participation_mode": "local_user",
        "adapter_id": "local-user",
        "joined_at": timestamp,
        "retired_at": None,
    }


def _agent_seat(spec: Mapping[str, str], timestamp: str) -> dict[str, Any]:
    return {
        "id": spec["id"],
        "kind": "agent",
        "display_name": spec["display_name"],
        "role_label": spec["role_label"],
        "active": True,
        "visible": True,
        "receives_broadcast": True,
        "can_own_tasks": True,
        # 建席不等于外部 Agent 已接入，默认只声明队列能力。
        "participation_mode": "queue_only",
        "adapter_id": "generic-queue",
        "joined_at": timestamp,
        "retired_at": None,
    }


class PanelStore:
    """项目本地状态存储；所有读写都限定在该项目的隐藏目录。"""

    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        root = Path(project_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValidationError("项目根目录必须是已存在的目录")
        self.project_root = root
        self.state_dir = root / STATE_DIR_NAME
        self.config_path = self.state_dir / "config.json"
        self.roster_path = self.state_dir / "roster.json"
        self.panel_events_path = self.state_dir / "panel-events.jsonl"
        self.roster_events_path = self.state_dir / "roster-events.jsonl"
        self.messages_path = self.state_dir / "messages.jsonl"
        self.tasks_path = self.state_dir / "tasks.jsonl"
        self.routing_path = self.state_dir / "routing-display.jsonl"
        self.cursors_dir = self.state_dir / "cursors"
        self.app_dir = self.state_dir / "app"
        self.lock_path = self.state_dir / ".lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if not self.state_dir.exists():
            raise NotFoundError("协作面板尚未初始化")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def initialize(
        self,
        *,
        project_name: str | None = None,
        user_name: str = "root",
        agents: Sequence[Mapping[str, Any]] | None = None,
        max_agent_seats: int = DEFAULT_MAX_AGENT_SEATS,
        port: int = 0,
    ) -> dict[str, Any]:
        if isinstance(max_agent_seats, bool) or not isinstance(max_agent_seats, int):
            raise ValidationError("Agent 席位上限必须是整数")
        if not MIN_AGENT_SEATS <= max_agent_seats <= MAX_AGENT_SEATS:
            raise ValidationError("Agent 席位上限必须在 1 到 32 之间")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValidationError("端口必须在 0 到 65535 之间")

        raw_agents = list(agents) if agents is not None else _default_agent_specs()[:max_agent_seats]
        normalized_agents = _normalize_agent_specs(raw_agents)
        if not MIN_AGENT_SEATS <= len(normalized_agents) <= max_agent_seats:
            raise ValidationError(f"初始化时必须配置 1 到 {max_agent_seats} 个 Agent")

        clean_name = (project_name or self.project_root.name).strip() or self.project_root.name
        if len(clean_name) > 80 or "\n" in clean_name or "\r" in clean_name:
            raise ValidationError("项目名称必须是 80 个字符以内的单行文本")
        clean_user_name = _require_plain_text(user_name, "用户显示名称", max_length=64)

        if self.state_dir.exists():
            raise RevisionConflict("项目已初始化协作面板")

        self.state_dir.mkdir(mode=0o700)
        self.cursors_dir.mkdir(mode=0o700)
        self.app_dir.mkdir(mode=0o700)
        timestamp = utc_now()
        project_hash = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()[:16]
        project_id = f"project-{project_hash}"

        roster = {
            "schema_version": ROSTER_SCHEMA,
            "project_id": project_id,
            "revision": 1,
            "max_agent_seats": max_agent_seats,
            "history_mode": "append_only",
            "seat_changes_grant_authority": False,
            "updated_at": timestamp,
            "updated_by": "user",
            "seats": [_user_seat(timestamp, clean_user_name)]
            + [_agent_seat(spec, timestamp) for spec in normalized_agents],
        }
        config = {
            "schema_version": CONFIG_SCHEMA,
            "project_id": project_id,
            "project_name": clean_name,
            "project_root": str(self.project_root),
            "host": LOOPBACK_HOST,
            "port": port,
            "api_token": uuid.uuid4().hex + uuid.uuid4().hex,
            "created_at": timestamp,
            "data_policy": "project_local_append_only",
            "routing_policy": "display_only_no_skill_invocation",
        }

        for log_path in (
            self.panel_events_path,
            self.roster_events_path,
            self.messages_path,
            self.tasks_path,
            self.routing_path,
        ):
            descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)

        _atomic_write_json(self.config_path, config)
        _atomic_write_json(self.roster_path, roster)
        for seat in roster["seats"]:
            self._write_cursor(
                seat["id"],
                last_seen_id=None,
                joined_after_message_id=None,
                joined_at=timestamp,
            )

        self._copy_assets()
        panel_event = self._panel_event(
            action="initialize",
            seat_id="user",
            reason="初始化项目本地协作面板",
            before=None,
            after=roster,
        )
        _append_jsonl(self.panel_events_path, panel_event)
        for seat in roster["seats"]:
            _append_jsonl(
                self.roster_events_path,
                self._roster_event(
                    action="initialize",
                    seat_id=seat["id"],
                    reason="初始化用户席" if seat["kind"] == "user" else "初始化默认 Agent 席",
                    before=None,
                    after=roster,
                ),
            )
        return self.snapshot()

    def _copy_assets(self) -> None:
        source = Path(__file__).resolve().parent.parent / "assets" / "multi-agent-panel"
        required = ("index.html", "app.js", "styles.css")
        for filename in required:
            source_path = source / filename
            if not source_path.is_file():
                raise PanelError(f"缺少面板静态资源：{filename}")
            destination = self.app_dir / filename
            shutil.copyfile(source_path, destination)
            os.chmod(destination, 0o600)

    def _load_config(self) -> dict[str, Any]:
        config = _read_json(self.config_path)
        if config.get("project_root") != str(self.project_root):
            raise PanelError("项目根路径与面板配置不一致；拒绝跨项目复用")
        if config.get("host") != LOOPBACK_HOST:
            raise PanelError("面板配置不是 127.0.0.1 回环地址")
        return config

    def _load_roster(self) -> dict[str, Any]:
        config = self._load_config()
        roster = _read_json(self.roster_path)
        if roster.get("schema_version") != ROSTER_SCHEMA:
            raise PanelError("不支持的席位表版本")
        if roster.get("project_id") != config.get("project_id"):
            raise PanelError("席位表与项目配置的 project_id 不一致")
        self._validate_roster_projection(roster)
        self._verify_event_history(roster)
        return roster

    @staticmethod
    def _validate_roster_projection(roster: Mapping[str, Any]) -> None:
        revision = roster.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise PanelError("席位表 revision 必须是正整数")
        maximum = roster.get("max_agent_seats")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_AGENT_SEATS:
            raise PanelError("席位表 max_agent_seats 必须在 1 到 32 之间")
        if roster.get("history_mode") != "append_only" or roster.get("seat_changes_grant_authority") is not False:
            raise PanelError("席位表违反追加历史或权限边界")
        seats = roster.get("seats")
        if not isinstance(seats, list):
            raise PanelError("席位表 seats 必须是数组")
        ids = [seat.get("id") for seat in seats if isinstance(seat, dict)]
        if len(ids) != len(seats) or len(ids) != len(set(ids)):
            raise PanelError("席位 ID 必须全局唯一")
        user_seats = [seat for seat in seats if seat.get("kind") == "user"]
        if len(user_seats) != 1 or user_seats[0].get("id") != "user":
            raise PanelError("席位表必须恰好包含一个 id=user 的用户席")
        user = user_seats[0]
        if user.get("active") is not True or user.get("retired_at") is not None:
            raise PanelError("用户席必须永久 active 且不得有 retired_at")
        if user.get("visible") is not True:
            raise PanelError("用户席必须保持可见")

        agents = [seat for seat in seats if seat.get("kind") == "agent"]
        if any(seat.get("kind") not in {"user", "agent"} for seat in seats):
            raise PanelError("该运行时只允许 user 与 agent 两类席位")
        if len(agents) > MAX_RETAINED_SEAT_RECORDS:
            raise PanelError("Agent 活动记录与墓碑总数超过 32")
        active_count = 0
        for seat in agents:
            seat_id = seat.get("id")
            if not isinstance(seat_id, str) or not SEAT_ID_RE.fullmatch(seat_id) or seat_id in RESERVED_AGENT_IDS:
                raise PanelError("Agent 席位包含无效或保留 ID")
            active = seat.get("active")
            retired_at = seat.get("retired_at")
            if active is True:
                active_count += 1
                if retired_at is not None:
                    raise PanelError("活动 Agent 不得设置 retired_at")
            elif active is False:
                if not isinstance(retired_at, str) or not retired_at:
                    raise PanelError("停用 Agent 必须保留 retired_at 墓碑")
                if seat.get("visible") is not False:
                    raise PanelError("停用 Agent 墓碑必须停止显示为活动席")
            else:
                raise PanelError("Agent active 必须是布尔值")
        if not MIN_AGENT_SEATS <= active_count <= maximum:
            raise PanelError("活动 Agent 数必须在 1 与 max_agent_seats 之间")

    def _verify_event_history(self, roster: Mapping[str, Any]) -> None:
        events = _read_jsonl(self.panel_events_path)
        if not events:
            raise PanelError("缺少 append-only 席位主事件日志")
        previous_hash: str | None = None
        previous_projection: Mapping[str, Any] | None = None
        historical_ids: set[str] = set()
        projections_by_revision: dict[int, Mapping[str, Any]] = {}
        for index, event in enumerate(events):
            if event.get("schema_version") != PANEL_EVENT_SCHEMA:
                raise PanelError("席位主事件日志版本不受支持", details={"index": index})
            event_hash = event.get("event_sha256")
            without_hash = dict(event)
            without_hash.pop("event_sha256", None)
            if not isinstance(event_hash, str) or sha256_tag(without_hash) != event_hash:
                raise PanelError("席位主事件 event_sha256 校验失败", details={"index": index})
            if event.get("prev_event_sha256") != previous_hash:
                raise PanelError("席位主事件哈希链断裂", details={"index": index})
            projection = event.get("roster_projection")
            if not isinstance(projection, dict):
                raise PanelError("席位主事件缺少 roster_projection", details={"index": index})
            self._validate_roster_projection(projection)
            if event.get("after_sha256") != sha256_tag(projection):
                raise PanelError("席位主事件 after_sha256 与投影不一致", details={"index": index})
            current_ids = {seat["id"] for seat in projection["seats"]}
            if not historical_ids.issubset(current_ids):
                raise PanelError("历史席位 ID 被删除；墓碑必须永久保留", details={"index": index})
            historical_ids.update(current_ids)
            revision = projection["revision"]
            if previous_projection is None:
                if (
                    event.get("action") != "initialize"
                    or event.get("previous_revision") is not None
                    or event.get("before_sha256") is not None
                    or revision != 1
                ):
                    raise PanelError("首个席位事件必须以 revision=1 初始化")
            else:
                expected_revision = previous_projection["revision"] + 1
                if revision != expected_revision or event.get("revision") != expected_revision:
                    raise PanelError("席位事件 revision 必须精确递增 1", details={"index": index})
                if event.get("previous_revision") != previous_projection["revision"]:
                    raise PanelError("席位事件 previous_revision 不匹配", details={"index": index})
                if event.get("before_sha256") != sha256_tag(previous_projection):
                    raise PanelError("席位事件 before_sha256 实算不匹配", details={"index": index})
            if event.get("revision") != revision:
                raise PanelError("席位事件 revision 与投影不一致", details={"index": index})
            projections_by_revision[revision] = projection
            previous_hash = event_hash
            previous_projection = projection

        if previous_projection is None or sha256_tag(previous_projection) != sha256_tag(roster):
            raise PanelError("roster.json 不是 append-only 主事件的最新投影")

        # 同时实算 roster-events 的事件哈希链与 before/after 哈希。
        previous_roster_event_hash: str | None = None
        for index, event in enumerate(_read_jsonl(self.roster_events_path)):
            if event.get("schema_version") != ROSTER_EVENT_SCHEMA:
                raise PanelError("roster-events 版本不受支持", details={"index": index})
            event_hash = event.get("event_sha256")
            unsigned_event = dict(event)
            unsigned_event.pop("event_sha256", None)
            if not isinstance(event_hash, str) or sha256_tag(unsigned_event) != event_hash:
                raise PanelError("roster-events event_sha256 校验失败", details={"index": index})
            if event.get("previous_event_sha256") != previous_roster_event_hash:
                raise PanelError("roster-events 哈希链断裂", details={"index": index})
            revision = event.get("revision")
            projection = projections_by_revision.get(revision)
            if projection is None or event.get("after_sha256") != sha256_tag(projection):
                raise PanelError("roster-events after_sha256 实算不匹配", details={"index": index})
            previous_revision = event.get("previous_revision")
            if previous_revision is None:
                if revision != 1 or event.get("before_sha256") is not None:
                    raise PanelError("roster-events 初始化哈希不合法", details={"index": index})
            else:
                previous = projections_by_revision.get(previous_revision)
                if previous is None or revision != previous_revision + 1:
                    raise PanelError("roster-events revision 链不合法", details={"index": index})
                if event.get("before_sha256") != sha256_tag(previous):
                    raise PanelError("roster-events before_sha256 实算不匹配", details={"index": index})
            previous_roster_event_hash = event_hash

    @staticmethod
    def _seat(roster: Mapping[str, Any], seat_id: str) -> dict[str, Any]:
        for seat in roster.get("seats", []):
            if seat.get("id") == seat_id:
                return seat
        raise NotFoundError(f"席位不存在：{seat_id}")

    @staticmethod
    def _active_agent_count(roster: Mapping[str, Any]) -> int:
        return sum(1 for seat in roster.get("seats", []) if seat.get("kind") == "agent" and seat.get("active"))

    @staticmethod
    def _check_revision(roster: Mapping[str, Any], expected_revision: Any) -> int:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValidationError("expected_revision 必须是整数")
        actual = roster.get("revision")
        if expected_revision != actual:
            raise RevisionConflict(
                "席位表已被其他操作更新，请刷新后重试",
                details={"expected_revision": expected_revision, "actual_revision": actual},
            )
        return actual

    def _panel_event(
        self,
        *,
        action: str,
        seat_id: str,
        reason: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = _read_jsonl(self.panel_events_path)
        previous_event_hash = existing[-1].get("event_sha256") if existing else None
        event = {
            "schema_version": PANEL_EVENT_SCHEMA,
            "event_id": "event-" + uuid.uuid4().hex,
            "timestamp": utc_now(),
            "actor": "user",
            "action": action,
            "seat_id": seat_id,
            "reason": reason,
            "previous_revision": None if before is None else before["revision"],
            "revision": after["revision"],
            "before_sha256": None if before is None else sha256_tag(before),
            "after_sha256": sha256_tag(after),
            "prev_event_sha256": previous_event_hash,
            # 完整投影使 append-only 主日志可恢复最新 roster。
            "roster_projection": copy.deepcopy(after),
        }
        if metadata:
            event["metadata"] = copy.deepcopy(dict(metadata))
        event["event_sha256"] = sha256_tag(event)
        return event

    def _roster_event(
        self,
        *,
        action: str,
        seat_id: str,
        reason: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = _read_jsonl(self.roster_events_path)
        previous_event_hash = existing[-1].get("event_sha256") if existing else None
        event = {
            "schema_version": ROSTER_EVENT_SCHEMA,
            "event_id": "event-" + uuid.uuid4().hex,
            "timestamp": utc_now(),
            "actor": "user",
            "action": action,
            "seat_id": seat_id,
            "reason": reason,
            "previous_revision": None if before is None else before["revision"],
            "revision": after["revision"],
            "before_sha256": None if before is None else sha256_tag(before),
            "after_sha256": sha256_tag(after),
            "previous_event_sha256": previous_event_hash,
        }
        event["event_sha256"] = sha256_tag(event)
        return event

    def _commit_roster(
        self,
        *,
        before: Mapping[str, Any],
        after: dict[str, Any],
        action: str,
        seat_id: str,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        after["revision"] = int(before["revision"]) + 1
        after["updated_at"] = utc_now()
        after["updated_by"] = "user"
        # 先追加完整的主事件，再写投影；即使中途退出，事件中仍保留可恢复状态。
        _append_jsonl(
            self.panel_events_path,
            self._panel_event(
                action=action,
                seat_id=seat_id,
                reason=reason,
                before=before,
                after=after,
                metadata=metadata,
            ),
        )
        _append_jsonl(
            self.roster_events_path,
            self._roster_event(
                action=action,
                seat_id=seat_id,
                reason=reason,
                before=before,
                after=after,
            ),
        )
        _atomic_write_json(self.roster_path, after)

    def _cursor_path(self, seat_id: str) -> Path:
        if seat_id != "user" and not SEAT_ID_RE.fullmatch(seat_id):
            raise ValidationError("无效席位 ID")
        return self.cursors_dir / f"{seat_id}.json"

    def _write_cursor(
        self,
        seat_id: str,
        *,
        last_seen_id: str | None,
        joined_after_message_id: str | None,
        joined_at: str,
    ) -> dict[str, Any]:
        cursor = {
            "schema_version": CURSOR_SCHEMA,
            "seat_id": seat_id,
            "last_seen_id": last_seen_id,
            "joined_after_message_id": joined_after_message_id,
            "joined_at": joined_at,
            "updated_at": utc_now(),
        }
        _atomic_write_json(self._cursor_path(seat_id), cursor)
        return cursor

    def _read_cursor(self, seat_id: str) -> dict[str, Any]:
        return _read_json(self._cursor_path(seat_id))

    def add_agent(
        self,
        *,
        seat_id: str,
        display_name: str,
        role_label: str = "协作 Agent",
        expected_revision: int,
        reason: str = "用户增加 Agent 席位",
    ) -> dict[str, Any]:
        normalized_id = _validate_agent_id(seat_id)
        name = _require_plain_text(display_name, "显示名称", max_length=64)
        role = _require_plain_text(role_label, "角色说明", max_length=64)
        clean_reason = _require_plain_text(reason, "调整原因", max_length=256)
        with self._locked():
            roster = self._load_roster()
            self._check_revision(roster, expected_revision)
            if any(seat.get("id") == normalized_id for seat in roster["seats"]):
                raise SeatGuardError(
                    "该 Agent ID 已存在或已形成墓碑，不能复用",
                    details={"seat_id": normalized_id, "id_reuse_allowed": False},
                )
            if self._active_agent_count(roster) >= roster["max_agent_seats"]:
                raise SeatGuardError(
                    "活动 Agent 席位已达到项目上限",
                    details={"max_agent_seats": roster["max_agent_seats"]},
                )
            agent_records = [seat for seat in roster["seats"] if seat.get("kind") == "agent"]
            if len(agent_records) >= MAX_RETAINED_SEAT_RECORDS:
                raise SeatGuardError("Agent 墓碑与活动记录总数已达到 32；需人工迁移后再扩展")

            messages = _read_jsonl(self.messages_path)
            baseline = messages[-1]["id"] if messages else None
            joined_at = utc_now()
            self._write_cursor(
                normalized_id,
                last_seen_id=baseline,
                joined_after_message_id=baseline,
                joined_at=joined_at,
            )
            updated = copy.deepcopy(roster)
            updated["seats"].append(
                _agent_seat(
                    {"id": normalized_id, "display_name": name, "role_label": role},
                    joined_at,
                )
            )
            self._commit_roster(
                before=roster,
                after=updated,
                action="add",
                seat_id=normalized_id,
                reason=clean_reason,
            )
        return self.agent_status(normalized_id)

    def disable_agent(
        self,
        *,
        seat_id: str,
        expected_revision: int,
        reason: str = "用户停用 Agent 席位",
    ) -> dict[str, Any]:
        if seat_id == "user":
            raise SeatGuardError("用户席是固定决策席，不可停用", details={"seat_id": "user"})
        normalized_id = _validate_agent_id(seat_id)
        clean_reason = _require_plain_text(reason, "停用原因", max_length=256)
        with self._locked():
            roster = self._load_roster()
            self._check_revision(roster, expected_revision)
            seat = self._seat(roster, normalized_id)
            if seat.get("kind") != "agent":
                raise SeatGuardError("只能停用 Agent 席位")
            if not seat.get("active"):
                raise SeatGuardError("该席位已经停用并形成墓碑", details={"seat_id": normalized_id})
            if self._active_agent_count(roster) <= MIN_AGENT_SEATS:
                raise SeatGuardError("至少保留 1 个活动 Agent 席位")

            self._guard_wakeup_retirement_unlocked(roster=roster, seat_id=normalized_id)

            pending = self._pending_messages_unlocked(normalized_id, roster=roster)
            open_tasks = self._open_tasks_for_unlocked(normalized_id)
            if pending or open_tasks:
                raise SeatGuardError(
                    "该席位仍有未处理消息或未关闭任务，不能停用",
                    details={
                        "pending_message_ids": [message["id"] for message in pending],
                        "open_task_ids": [task["id"] for task in open_tasks],
                    },
                )

            updated = copy.deepcopy(roster)
            updated_seat = self._seat(updated, normalized_id)
            updated_seat["active"] = False
            updated_seat["visible"] = False
            updated_seat["retired_at"] = utc_now()
            self._commit_roster(
                before=roster,
                after=updated,
                action="retire",
                seat_id=normalized_id,
                reason=clean_reason,
            )
        return self.agent_status(normalized_id, include_retired=True)

    def set_visibility(
        self,
        *,
        seat_id: str,
        visible: bool,
        expected_revision: int,
        reason: str = "用户调整 Agent 席位显示状态",
    ) -> dict[str, Any]:
        if seat_id == "user":
            raise SeatGuardError("用户席必须保持可见", details={"seat_id": "user"})
        if not isinstance(visible, bool):
            raise ValidationError("visible 必须是布尔值")
        normalized_id = _validate_agent_id(seat_id)
        clean_reason = _require_plain_text(reason, "显示调整原因", max_length=256)
        with self._locked():
            roster = self._load_roster()
            self._check_revision(roster, expected_revision)
            seat = self._seat(roster, normalized_id)
            if seat.get("kind") != "agent" or not seat.get("active"):
                raise SeatGuardError("停用墓碑不能直接显示；请先恢复原席位")
            if seat.get("visible") is visible:
                raise SeatGuardError("席位已经处于请求的显示状态")
            updated = copy.deepcopy(roster)
            self._seat(updated, normalized_id)["visible"] = visible
            self._commit_roster(
                before=roster,
                after=updated,
                action="show" if visible else "hide",
                seat_id=normalized_id,
                reason=clean_reason,
            )
        return self.agent_status(normalized_id)

    def restore_agent(
        self,
        *,
        seat_id: str,
        expected_revision: int,
        reason: str = "用户恢复原 Agent 墓碑席位",
    ) -> dict[str, Any]:
        if seat_id == "user":
            raise SeatGuardError("用户席始终活动，无需恢复", details={"seat_id": "user"})
        normalized_id = _validate_agent_id(seat_id)
        clean_reason = _require_plain_text(reason, "恢复原因", max_length=256)
        with self._locked():
            roster = self._load_roster()
            self._check_revision(roster, expected_revision)
            seat = self._seat(roster, normalized_id)
            if seat.get("kind") != "agent" or seat.get("active"):
                raise SeatGuardError("只有已停用的 Agent 墓碑可以恢复")
            if self._active_agent_count(roster) >= roster["max_agent_seats"]:
                raise SeatGuardError(
                    "活动 Agent 席位已达到项目上限，不能恢复",
                    details={"max_agent_seats": roster["max_agent_seats"]},
                )

            messages = _read_jsonl(self.messages_path)
            baseline = messages[-1]["id"] if messages else None
            previous_cursor = self._read_cursor(normalized_id)
            restored_cursor = self._write_cursor(
                normalized_id,
                last_seen_id=baseline,
                joined_after_message_id=previous_cursor.get("joined_after_message_id"),
                joined_at=previous_cursor["joined_at"],
            )
            updated = copy.deepcopy(roster)
            updated_seat = self._seat(updated, normalized_id)
            updated_seat["active"] = True
            updated_seat["visible"] = True
            updated_seat["retired_at"] = None
            self._commit_roster(
                before=roster,
                after=updated,
                action="restore",
                seat_id=normalized_id,
                reason=clean_reason,
                metadata={"cursor_resumed_after_message_id": baseline},
            )
        result = self.agent_status(normalized_id)
        result["restored_cursor"] = restored_cursor
        return result

    def send_message(
        self,
        *,
        speaker: str,
        audience: str,
        body: str,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(body, str) or not body.strip():
            raise ValidationError("消息正文不能为空")
        clean_body = body.strip()
        if len(clean_body) > MAX_TEXT_LENGTH:
            raise ValidationError(f"消息正文不能超过 {MAX_TEXT_LENGTH} 个字符")
        if reply_to is not None:
            reply_to = _validate_opaque_id(reply_to, "reply_to")
        with self._locked():
            roster = self._load_roster()
            valid_speakers = {
                seat["id"] for seat in roster["seats"] if seat.get("active") and seat.get("id") != "system"
            }
            if speaker not in valid_speakers:
                raise ValidationError("speaker 必须是用户席或活动 Agent 席")
            valid_audiences = {"all", "user"} | {
                seat["id"]
                for seat in roster["seats"]
                if seat.get("kind") == "agent" and seat.get("active")
            }
            if audience not in valid_audiences:
                raise ValidationError("audience 必须是 all、user 或活动 Agent ID")
            if reply_to is not None and not any(
                item.get("id") == reply_to for item in _read_jsonl(self.messages_path)
            ):
                raise ValidationError("reply_to 指向的消息不存在")
            message = {
                "schema_version": MESSAGE_SCHEMA,
                "id": "msg-" + uuid.uuid4().hex,
                "timestamp": utc_now(),
                "speaker": speaker,
                "audience": audience,
                "body": clean_body,
                "reply_to": reply_to,
                "delivery": "queued" if audience != "user" else "recorded",
            }
            _append_jsonl(self.messages_path, message)
            return message

    def _messages_after_cursor(self, cursor_id: str | None) -> list[dict[str, Any]]:
        messages = _read_jsonl(self.messages_path)
        if cursor_id is None:
            return messages
        for index, message in enumerate(messages):
            if message.get("id") == cursor_id:
                return messages[index + 1 :]
        raise PanelError("席位游标指向不存在的消息；拒绝静默跳过")

    def _pending_messages_unlocked(
        self, seat_id: str, *, roster: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        active_roster = roster or self._load_roster()
        seat = self._seat(active_roster, seat_id)
        if seat.get("kind") != "agent":
            raise ValidationError("只有 Agent 席位有待处理队列")
        cursor = self._read_cursor(seat_id)
        return [
            message
            for message in self._messages_after_cursor(cursor.get("last_seen_id"))
            if message.get("speaker") != seat_id
            and (
                message.get("audience") == seat_id
                or (message.get("audience") == "all" and seat.get("receives_broadcast") is True)
            )
        ]

    def inbox(self, seat_id: str, *, include_retired: bool = False) -> dict[str, Any]:
        normalized_id = _validate_agent_id(seat_id)
        with self._locked():
            roster = self._load_roster()
            seat = self._seat(roster, normalized_id)
            if not seat.get("active") and not include_retired:
                raise SeatGuardError("停用席位不能继续读取新队列")
            cursor = self._read_cursor(normalized_id)
            messages = self._pending_messages_unlocked(normalized_id, roster=roster)
            return {
                "seat_id": normalized_id,
                "cursor": cursor,
                "pending_count": len(messages),
                "messages": messages,
            }

    def acknowledge(self, *, seat_id: str, message_id: str) -> dict[str, Any]:
        normalized_id = _validate_agent_id(seat_id)
        normalized_message_id = _validate_opaque_id(message_id, "message_id")
        with self._locked():
            roster = self._load_roster()
            seat = self._seat(roster, normalized_id)
            if not seat.get("active"):
                raise SeatGuardError("停用席位不能推进游标")
            self._guard_wakeup_ack_unlocked(
                seat_id=normalized_id,
                message_id=normalized_message_id,
            )
            pending = self._pending_messages_unlocked(normalized_id, roster=roster)
            pending_ids = [message["id"] for message in pending]
            if normalized_message_id not in pending_ids:
                raise ValidationError("只能确认当前待处理队列中的精确消息 ID")
            old_cursor = self._read_cursor(normalized_id)
            return self._write_cursor(
                normalized_id,
                last_seen_id=normalized_message_id,
                joined_after_message_id=old_cursor.get("joined_after_message_id"),
                joined_at=old_cursor["joined_at"],
            )

    def _guard_wakeup_ack_unlocked(self, *, seat_id: str, message_id: str) -> None:
        """Caller holds panel lock; acquire only the second-order wake lock."""

        wakeup_dir = self.state_dir / "wakeup"
        if not wakeup_dir.is_dir():
            return
        module_path = Path(__file__).resolve().with_name("agent_wakeup.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "sublation_agent_wakeup_ack_guard",
                module_path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("wakeup module spec unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.WakeupStore(self.project_root).guard_panel_ack(
                seat_id=seat_id,
                message_id=message_id,
                panel_lock_held=True,
            )
        except Exception as exc:
            raise WakeLeaseConflict(
                "自动唤醒意图、租约或恢复事务尚未结束，普通 ACK 不得推进游标",
                details={
                    "seat_id": seat_id,
                    "message_id": message_id,
                    "wakeup_error_code": getattr(exc, "code", type(exc).__name__),
                },
            ) from exc

    def add_task(
        self,
        *,
        title: str,
        owner: str,
        status: str = "todo",
        note: str = "",
    ) -> dict[str, Any]:
        clean_title = _require_plain_text(title, "任务标题", max_length=256)
        if status not in TASK_STATUSES:
            raise ValidationError("无效任务状态")
        if not isinstance(note, str) or len(note) > 2_000:
            raise ValidationError("任务备注必须是 2000 字以内文本")
        with self._locked():
            roster = self._load_roster()
            valid_owners = {
                seat["id"] for seat in roster["seats"] if seat.get("active") and seat.get("can_own_tasks")
            }
            if owner not in valid_owners:
                raise ValidationError("任务 owner 必须是用户席或活动 Agent 席")
            event = {
                "schema_version": TASK_EVENT_SCHEMA,
                "id": "task-" + uuid.uuid4().hex,
                "event_id": "task-event-" + uuid.uuid4().hex,
                "timestamp": utc_now(),
                "actor": "user",
                "op": "create",
                "version": 1,
                "title": clean_title,
                "owner": owner,
                "status": status,
                "note": note,
            }
            _append_jsonl(self.tasks_path, event)
            return event

    def update_task(self, *, task_id: str, status: str, note: str = "") -> dict[str, Any]:
        normalized_task_id = _validate_opaque_id(task_id, "task_id")
        if status not in TASK_STATUSES:
            raise ValidationError("无效任务状态")
        if not isinstance(note, str) or len(note) > 2_000:
            raise ValidationError("任务备注必须是 2000 字以内文本")
        with self._locked():
            tasks = self._latest_tasks_unlocked()
            previous = next((item for item in tasks if item["id"] == normalized_task_id), None)
            if previous is None:
                raise NotFoundError("任务不存在")
            event = {
                "schema_version": TASK_EVENT_SCHEMA,
                "id": previous["id"],
                "event_id": "task-event-" + uuid.uuid4().hex,
                "timestamp": utc_now(),
                "actor": "user",
                "op": "update",
                "version": int(previous["version"]) + 1,
                "title": previous["title"],
                "owner": previous["owner"],
                "status": status,
                "note": note,
            }
            _append_jsonl(self.tasks_path, event)
            return event

    def _latest_tasks_unlocked(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in _read_jsonl(self.tasks_path):
            task_id = event["id"]
            if task_id not in latest:
                order.append(task_id)
            latest[task_id] = event
        return [latest[task_id] for task_id in order]

    def _open_tasks_for_unlocked(self, seat_id: str) -> list[dict[str, Any]]:
        return [
            task
            for task in self._latest_tasks_unlocked()
            if task.get("owner") == seat_id and task.get("status") in OPEN_TASK_STATUSES
        ]

    def record_routing_status(
        self,
        *,
        state: str,
        request_id: str,
        skill_ids: Sequence[str] = (),
        agent_id: str | None = None,
        note: str = "",
        host_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in ROUTING_STATES:
            raise ValidationError("路由状态只能是建议、待澄清、已调用或已完成")
        if state in {"invoked", "completed"}:
            raise HostAdapterRequired(
                "V5 尚无可验证签名的宿主适配器；拒绝记录已调用/已完成",
                details={
                    "state": state,
                    "actual_invocation": "not_performed",
                    "future_contract": "routing-invocation-receipt-v1.json",
                },
            )
        clean_request_id = _validate_opaque_id(request_id, "request_id")
        if isinstance(skill_ids, (str, bytes)) or not isinstance(skill_ids, Sequence):
            raise ValidationError("skill_ids 必须是字符串数组")
        clean_skills: list[str] = []
        for skill_id in skill_ids:
            clean_skills.append(_require_plain_text(skill_id, "skill_id", max_length=128))
        if len(clean_skills) > 20:
            raise ValidationError("单条路由展示最多记录 20 个 Skill ID")
        if not isinstance(note, str) or len(note) > 2_000:
            raise ValidationError("路由备注必须是 2000 字以内文本")

        with self._locked():
            roster = self._load_roster()
            clean_agent_id: str | None = None
            if agent_id is not None:
                clean_agent_id = _validate_agent_id(agent_id)
                seat = self._seat(roster, clean_agent_id)
                if seat.get("kind") != "agent" or not seat.get("active"):
                    raise ValidationError("agent_id 必须指向活动 Agent 席位")
            validated_receipt: dict[str, Any] | None = None
            if state in {"invoked", "completed"}:
                if clean_agent_id is None:
                    raise ValidationError("已调用/已完成状态必须绑定 Agent 席位")
                validated_receipt = self._validate_host_routing_receipt(
                    host_receipt,
                    expected_state=state,
                    request_id=clean_request_id,
                    agent_id=clean_agent_id,
                    skill_ids=clean_skills,
                )
            elif host_receipt is not None:
                raise ValidationError("建议/待澄清阶段不接受调用回执")
            record = {
                "schema_version": ROUTING_DISPLAY_SCHEMA,
                "id": "route-display-" + uuid.uuid4().hex,
                "timestamp": utc_now(),
                "state": state,
                "state_label": ROUTING_STATES[state],
                "request_id": clean_request_id,
                "skill_ids": clean_skills,
                "agent_id": clean_agent_id,
                "note": note,
                "host_receipt_id": None if validated_receipt is None else validated_receipt["receipt_id"],
                "host_receipt_sha256": None
                if validated_receipt is None
                else validated_receipt["receipt_sha256"],
                "baseline_sha256": None
                if validated_receipt is None
                else validated_receipt["baseline_sha256"],
                "effective_sha256": None
                if validated_receipt is None
                else validated_receipt["effective_sha256"],
                "receipt_validated": validated_receipt is not None,
                "host_invocation_performed": bool(validated_receipt),
                "display_only": True,
                "panel_invocation_performed": False,
            }
            # 这里没有 Skill 导入、子进程或网络调用；只追加宿主提供的展示回执。
            _append_jsonl(self.routing_path, record)
            return record

    @staticmethod
    def _validate_host_routing_receipt(
        receipt: Mapping[str, Any] | None,
        *,
        expected_state: str,
        request_id: str,
        agent_id: str,
        skill_ids: Sequence[str],
    ) -> dict[str, Any]:
        # 公开 SHA-256 只能校验内容自洽，不能证明回执来自真实宿主。
        # 在未来接入独立密钥/签名及可信 adapter 前，本入口必须始终 fail closed。
        raise HostAdapterRequired(
            "宿主回执尚无可信签名适配器，不能证明真实调用",
            details={
                "state": expected_state,
                "actual_invocation": "not_performed",
                "future_contract": "routing-invocation-receipt-v1.json",
            },
        )

    def record_routing_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"state", "request_id", "skill_ids", "agent_id", "note", "host_receipt"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(
                "路由展示接口拒绝执行性或未知字段",
                details={"unknown_fields": unknown, "display_only": True},
            )
        return self.record_routing_status(
            state=payload.get("state"),
            request_id=payload.get("request_id"),
            skill_ids=payload.get("skill_ids", ()),
            agent_id=payload.get("agent_id"),
            note=payload.get("note", ""),
            host_receipt=payload.get("host_receipt"),
        )

    def paged_messages(self, *, before_id: str | None = None, limit: int = 60) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 120:
            raise ValidationError("消息页大小必须在 1 到 120 之间")
        messages = _read_jsonl(self.messages_path)
        end = len(messages)
        if before_id is not None:
            normalized = _validate_opaque_id(before_id, "before_id")
            try:
                end = next(index for index, message in enumerate(messages) if message["id"] == normalized)
            except StopIteration as exc:
                raise NotFoundError("before_id 指向的消息不存在") from exc
        start = max(0, end - limit)
        return {"messages": messages[start:end], "has_older": start > 0}

    def agent_status(self, seat_id: str, *, include_retired: bool = False) -> dict[str, Any]:
        normalized_id = _validate_agent_id(seat_id)
        with self._locked():
            roster = self._load_roster()
            seat = copy.deepcopy(self._seat(roster, normalized_id))
            if not seat.get("active") and not include_retired:
                raise NotFoundError("活动 Agent 席位不存在")
            pending_count = 0
            if seat.get("active"):
                pending_count = len(self._pending_messages_unlocked(normalized_id, roster=roster))
            return {
                "seat": seat,
                "pending_count": pending_count,
                "open_task_count": len(self._open_tasks_for_unlocked(normalized_id)),
                "tombstoned": bool(seat.get("retired_at")),
                "id_reuse_allowed": False,
                "roster_revision": roster["revision"],
            }

    def _wakeup_snapshot_unlocked(self, roster: Mapping[str, Any]) -> dict[str, Any]:
        """只读投影受控唤醒状态；未初始化时保持 default-off。

        唤醒运行时使用独立文件锁。本方法不 arm、不 claim，也不调用任何
        Agent；导入失败或状态损坏时只返回 fail-closed 状态，避免 UI 把
        watcher/进程存在误报为已唤醒。
        """

        wakeup_dir = self.state_dir / "wakeup"
        default_seats = [
            {
                "seat_id": seat["id"],
                "active": bool(seat.get("active")),
                "enabled": False,
                "mode": "queue_only",
                "adapter_id": None,
                "pending_intent_id": None,
                "last_outcome": "not_initialized",
            }
            for seat in roster.get("seats", [])
            if seat.get("kind") == "agent"
        ]
        if not wakeup_dir.is_dir():
            return {
                "initialized": False,
                "status": "default_off",
                "wake_window": {
                    "state": "closed",
                    "pending_intents": 0,
                    "active_leases": 0,
                    "close_receipt_sha256": None,
                },
                "seats": default_seats,
                "totals": {
                    "enabled": 0,
                    "disabled": len(default_seats),
                    "open_intents": 0,
                },
                "event_chain": {"valid": True, "count": 0, "head_hash": None},
                "boundary": {
                    "direct_process_launch": False,
                    "external_network": False,
                    "arbitrary_command_fields": False,
                    "launchd_or_cron_installation": "out_of_scope",
                    "advance_only_on": "sent_and_verified",
                    "max_messages_per_wake": 1,
                },
            }

        module_path = Path(__file__).resolve().with_name("agent_wakeup.py")
        try:
            spec = importlib.util.spec_from_file_location("sublation_agent_wakeup", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("wakeup module spec unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Caller already holds the panel lock.  The wake runtime therefore
            # acquires only its second-order lock here (panel -> wakeup), never
            # the reverse order.
            projection = module.WakeupStore(self.project_root).panel_snapshot(
                panel_lock_held=True
            )
            projection["initialized"] = True
            projection["status"] = "ready"
            return projection
        except Exception as exc:  # 唤醒投影损坏不能被静默当作正常关闭。
            return {
                "initialized": True,
                "status": "hold_invalid_state",
                "error_code": getattr(exc, "code", type(exc).__name__),
                "wake_window": {
                    "state": "hold",
                    "pending_intents": None,
                    "active_leases": None,
                    "close_receipt_sha256": None,
                },
                "seats": default_seats,
                "totals": {"enabled": 0, "disabled": len(default_seats), "open_intents": None},
                "event_chain": {"valid": False, "count": None, "head_hash": None},
                "boundary": {
                    "direct_process_launch": False,
                    "external_network": False,
                    "arbitrary_command_fields": False,
                    "launchd_or_cron_installation": "out_of_scope",
                    "advance_only_on": "sent_and_verified",
                    "max_messages_per_wake": 1,
                },
            }

    def _guard_wakeup_retirement_unlocked(
        self,
        *,
        roster: Mapping[str, Any],
        seat_id: str,
    ) -> None:
        projection = self._wakeup_snapshot_unlocked(roster)
        if not projection.get("initialized"):
            return
        if projection.get("status") != "ready":
            raise SeatGuardError(
                "自动唤醒账本无法验证，拒绝停用席位",
                details={"wakeup_status": projection.get("status")},
            )
        seat_state = next(
            (item for item in projection.get("seats", []) if item.get("seat_id") == seat_id),
            None,
        )
        if seat_state is None:
            raise SeatGuardError(
                "自动唤醒账本缺少该席位投影，拒绝停用",
                details={"seat_id": seat_id},
            )
        if (
            seat_state.get("pending_intent_id")
            or seat_state.get("last_outcome") == "claimed"
            or seat_state.get("awaiting_user_disposition")
            or seat_state.get("completion_recovery_required")
        ):
            raise SeatGuardError(
                "该席位仍有自动唤醒意图或活动租约，不能停用",
                details={
                    "wakeup_intent_id": seat_state.get("pending_intent_id"),
                    "wakeup_last_outcome": seat_state.get("last_outcome"),
                    "awaiting_user_disposition": seat_state.get("awaiting_user_disposition"),
                    "completion_recovery_required": seat_state.get(
                        "completion_recovery_required"
                    ),
                },
            )

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            config = self._load_config()
            roster = self._load_roster()
            agent_runtime: dict[str, Any] = {}
            for seat in roster["seats"]:
                if seat.get("kind") != "agent":
                    continue
                pending_count = 0
                if seat.get("active"):
                    pending_count = len(self._pending_messages_unlocked(seat["id"], roster=roster))
                agent_runtime[seat["id"]] = {
                    "pending_count": pending_count,
                    "open_task_count": len(self._open_tasks_for_unlocked(seat["id"])),
                    "tombstoned": bool(seat.get("retired_at")),
                    "id_reuse_allowed": False,
                }
            page = self.paged_messages(limit=60)
            routing_records = _read_jsonl(self.routing_path)
            latest_routing: dict[str, dict[str, Any] | None] = {key: None for key in ROUTING_STATES}
            latest_advisory: dict[str, Any] | None = None
            for record in routing_records:
                state = record.get("state")
                # V5 没有可信签名宿主 adapter；即使日志被手工追加，也绝不投影高级状态。
                if state in {"suggested", "clarification_required"}:
                    latest_routing[state] = record
                    latest_advisory = record
            layer_records = {
                "metadata_classification": {
                    "status": "metadata_only" if latest_routing["suggested"] else "unavailable",
                    "record": latest_routing["suggested"],
                },
                "advisory_recommendation": {
                    "status": latest_advisory.get("state") if latest_advisory else "unavailable",
                    "record": latest_advisory,
                },
                "host_selection": {"status": "preserved", "record": None},
                "actual_invocation": {"status": "not_performed", "record": None},
            }
            return {
                "ok": True,
                "project": {
                    "id": config["project_id"],
                    "name": config["project_name"],
                    "host": config["host"],
                    "data_policy": config["data_policy"],
                },
                "roster": roster,
                "agent_runtime": agent_runtime,
                "messages": page["messages"],
                "has_older_messages": page["has_older"],
                "tasks": self._latest_tasks_unlocked(),
                "wakeup": self._wakeup_snapshot_unlocked(roster),
                "routing": {
                    "policy": "display_only_no_skill_invocation",
                    "raw_prompt_stored": False,
                    "authority_granted_by_router": False,
                    "states": ROUTING_STATES,
                    "latest_by_state": latest_routing,
                    "layers": [
                        {"id": layer_id, "label": label, **layer_records[layer_id]}
                        for layer_id, label in ROUTING_LAYERS
                    ],
                },
            }

    def api_token(self) -> str:
        return self._load_config()["api_token"]


class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store: PanelStore, port: int) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValidationError("端口必须在 0 到 65535 之间")
        self.store = store
        super().__init__((LOOPBACK_HOST, port), PanelRequestHandler)


class PanelRequestHandler(BaseHTTPRequestHandler):
    server: PanelHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[协作面板] " + (fmt % args) + "\n")

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        return host.split(":", 1)[0] == LOOPBACK_HOST

    def _token_allowed(self) -> bool:
        supplied = self.headers.get("X-Panel-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.server.store.api_token())

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        data = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_asset(self, filename: str, content_type: str) -> None:
        path = self.server.store.app_dir / filename
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, NotFoundError("静态资源不存在").as_dict())
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _reject_origin_or_host(self) -> bool:
        if self.client_address[0] != LOOPBACK_HOST or not self._host_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback_only", "message": "仅允许 127.0.0.1 本机访问"})
            return True
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname != LOOPBACK_HOST:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_rejected", "message": "拒绝非本面板来源"})
                return True
        return False

    def _require_api_token(self) -> bool:
        if not self._token_allowed():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "token_required", "message": "缺少或无效的项目面板令牌"})
            return False
        return True

    def _read_payload(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise ValidationError("请求 Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValidationError("请求正文大小无效")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValidationError("请求正文不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求 JSON 必须是对象")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "cors_disabled", "message": "面板不开放跨来源访问"})

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_origin_or_host():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_asset("index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self._send_asset("app.js", "text/javascript; charset=utf-8")
                return
            if parsed.path == "/styles.css":
                self._send_asset("styles.css", "text/css; charset=utf-8")
                return
            if parsed.path == "/api/health":
                config = self.server.store._load_config()
                root_sha256 = "sha256:" + hashlib.sha256(
                    str(self.server.store.project_root).encode("utf-8")
                ).hexdigest()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "host": LOOPBACK_HOST,
                        "project_id": config["project_id"],
                        "project_root_sha256": root_sha256,
                        "port": self.server.server_address[1],
                        "capabilities": {
                            "dynamic_roster": True,
                            "precise_inbox_ack": True,
                            "wakeup_intent_gate": True,
                            "direct_agent_launch": False,
                            "routing_invocation": False,
                        },
                    },
                )
                return
            if parsed.path == "/api/bootstrap":
                config = self.server.store._load_config()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "project_id": config["project_id"],
                        "api_token": config["api_token"],
                        "token_scope": "same_origin_panel_csrf_only",
                        "watcher_trust_root": False,
                    },
                )
                return
            if not self._require_api_token():
                return
            if parsed.path == "/api/snapshot":
                self._send_json(HTTPStatus.OK, self.server.store.snapshot())
                return
            if parsed.path == "/api/agents":
                snapshot = self.server.store.snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "roster": snapshot["roster"],
                        "agent_runtime": snapshot["agent_runtime"],
                    },
                )
                return
            if parsed.path == "/api/wakeup":
                snapshot = self.server.store.snapshot()
                self._send_json(HTTPStatus.OK, {"ok": True, "wakeup": snapshot["wakeup"]})
                return
            if parsed.path == "/api/messages":
                query = parse_qs(parsed.query)
                before = query.get("before_id", [None])[0]
                limit_raw = query.get("limit", ["120"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError as exc:
                    raise ValidationError("limit 必须是整数") from exc
                result = self.server.store.paged_messages(before_id=before, limit=limit)
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            inbox_match = re.fullmatch(r"/api/agents/([a-z0-9][a-z0-9._-]{0,63})/inbox", parsed.path)
            if inbox_match:
                result = self.server.store.inbox(inbox_match.group(1))
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            raise NotFoundError("接口不存在")
        except PanelError as exc:
            self._send_json(exc.status, exc.as_dict())

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_origin_or_host() or not self._require_api_token():
            return
        parsed = urlparse(self.path)
        try:
            payload = self._read_payload()
            if parsed.path == "/api/messages":
                result = self.server.store.send_message(
                    speaker=payload.get("speaker"),
                    audience=payload.get("audience"),
                    body=payload.get("body"),
                    reply_to=payload.get("reply_to"),
                )
                self._send_json(HTTPStatus.CREATED, {"ok": True, "message": result})
                return
            if parsed.path == "/api/agents":
                result = self.server.store.add_agent(
                    seat_id=payload.get("id"),
                    display_name=payload.get("display_name"),
                    role_label=payload.get("role_label", "协作 Agent"),
                    expected_revision=payload.get("expected_revision"),
                    reason=payload.get("reason", "用户通过面板增加 Agent 席位"),
                )
                self._send_json(HTTPStatus.CREATED, {"ok": True, **result})
                return
            disable_match = re.fullmatch(
                r"/api/agents/([a-z0-9][a-z0-9._-]{0,63})/(?:disable|retire)", parsed.path
            )
            if disable_match:
                result = self.server.store.disable_agent(
                    seat_id=disable_match.group(1),
                    expected_revision=payload.get("expected_revision"),
                    reason=payload.get("reason", "用户通过面板停用 Agent 席位"),
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            visibility_match = re.fullmatch(
                r"/api/agents/([a-z0-9][a-z0-9._-]{0,63})/(visibility|hide|show)", parsed.path
            )
            if visibility_match:
                action = visibility_match.group(2)
                visible = payload.get("visible") if action == "visibility" else action == "show"
                result = self.server.store.set_visibility(
                    seat_id=visibility_match.group(1),
                    visible=visible,
                    expected_revision=payload.get("expected_revision"),
                    reason=payload.get("reason", "用户通过面板调整 Agent 席位显示状态"),
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            restore_match = re.fullmatch(
                r"/api/agents/([a-z0-9][a-z0-9._-]{0,63})/restore", parsed.path
            )
            if restore_match:
                result = self.server.store.restore_agent(
                    seat_id=restore_match.group(1),
                    expected_revision=payload.get("expected_revision"),
                    reason=payload.get("reason", "用户通过面板恢复原 Agent 墓碑席位"),
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            ack_match = re.fullmatch(r"/api/agents/([a-z0-9][a-z0-9._-]{0,63})/ack", parsed.path)
            if ack_match:
                result = self.server.store.acknowledge(
                    seat_id=ack_match.group(1), message_id=payload.get("message_id")
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "cursor": result})
                return
            if parsed.path == "/api/tasks":
                result = self.server.store.add_task(
                    title=payload.get("title"),
                    owner=payload.get("owner"),
                    status=payload.get("status", "todo"),
                    note=payload.get("note", ""),
                )
                self._send_json(HTTPStatus.CREATED, {"ok": True, "task": result})
                return
            task_match = re.fullmatch(r"/api/tasks/([A-Za-z0-9._:-]{1,128})/status", parsed.path)
            if task_match:
                result = self.server.store.update_task(
                    task_id=task_match.group(1), status=payload.get("status"), note=payload.get("note", "")
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "task": result})
                return
            if parsed.path == "/api/routing":
                result = self.server.store.record_routing_payload(payload)
                self._send_json(HTTPStatus.CREATED, {"ok": True, "routing": result})
                return
            raise NotFoundError("接口不存在")
        except PanelError as exc:
            self._send_json(exc.status, exc.as_dict())


def make_server(store: PanelStore, *, port: int = 0) -> PanelHTTPServer:
    """构造只绑定 127.0.0.1 的 HTTP server，供 CLI 与测试共用。"""

    store._load_config()
    return PanelHTTPServer(store, port)


def _parse_agent_cli(value: str) -> dict[str, str]:
    parts = value.split(":", 2)
    seat_id = parts[0]
    display_name = parts[1] if len(parts) >= 2 and parts[1] else seat_id
    role_label = parts[2] if len(parts) >= 3 and parts[2] else "协作 Agent"
    return {"id": seat_id, "display_name": display_name, "role_label": role_label}


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sublation 项目本地多 Agent 协作面板")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="在项目根目录初始化面板")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--name")
    init_parser.add_argument("--user-name", default="root")
    init_parser.add_argument("--max-agent-seats", type=int, default=DEFAULT_MAX_AGENT_SEATS)
    init_parser.add_argument("--port", type=int, default=0)
    init_parser.add_argument(
        "--agent",
        action="append",
        default=None,
        metavar="ID:显示名:角色",
        help="可重复，未提供时使用默认三个队列席位",
    )

    serve_parser = subparsers.add_parser("serve", help="仅在 127.0.0.1 启动面板")
    serve_parser.add_argument("--project", required=True)
    serve_parser.add_argument("--host", choices=(LOOPBACK_HOST,), default=LOOPBACK_HOST)
    serve_parser.add_argument("--port", type=int, default=None)

    snapshot_parser = subparsers.add_parser("snapshot", help="输出当前投影")
    snapshot_parser.add_argument("--project", required=True)

    roster_parser = subparsers.add_parser("roster", help="输出动态席位表")
    roster_parser.add_argument("--project", required=True)

    validate_parser = subparsers.add_parser("validate", help="校验项目身份、席位投影与事件哈希链")
    validate_parser.add_argument("--project", required=True)

    add_parser = subparsers.add_parser("add-seat", aliases=["agent-add"], help="增加 Agent 席位")
    add_parser.add_argument("--project", required=True)
    add_parser.add_argument("--seat-id", "--id", dest="seat_id", required=True)
    add_parser.add_argument("--display-name", "--name", dest="display_name", required=True)
    add_parser.add_argument("--role-label", "--role", dest="role_label", default="协作 Agent")
    add_parser.add_argument("--expected-revision", type=int, required=True)
    add_parser.add_argument("--reason", default="用户通过 CLI 增加 Agent 席位")

    disable_parser = subparsers.add_parser(
        "retire-seat", aliases=["agent-disable"], help="安全停用并保留 Agent 墓碑"
    )
    disable_parser.add_argument("--project", required=True)
    disable_parser.add_argument("--seat-id", "--id", dest="seat_id", required=True)
    disable_parser.add_argument("--expected-revision", type=int, required=True)
    disable_parser.add_argument("--reason", default="用户通过 CLI 停用 Agent 席位")

    for command, help_text, default_reason in (
        ("hide-seat", "隐藏活动 Agent 卡片但继续队列投递", "用户通过 CLI 隐藏 Agent 席位"),
        ("show-seat", "重新显示已隐藏的活动 Agent 卡片", "用户通过 CLI 显示 Agent 席位"),
        ("restore-seat", "恢复原 Agent 墓碑并从当前消息末尾继续", "用户通过 CLI 恢复 Agent 席位"),
    ):
        seat_parser = subparsers.add_parser(command, help=help_text)
        seat_parser.add_argument("--project", required=True)
        seat_parser.add_argument("--seat-id", required=True)
        seat_parser.add_argument("--expected-revision", type=int, required=True)
        seat_parser.add_argument("--reason", default=default_reason)

    send_parser = subparsers.add_parser("send", help="追加一条消息")
    send_parser.add_argument("--project", required=True)
    send_parser.add_argument("--speaker", required=True)
    send_parser.add_argument("--audience", required=True)
    send_parser.add_argument("--body", required=True)

    inbox_parser = subparsers.add_parser("inbox", help="读取 Agent 待处理队列")
    inbox_parser.add_argument("--project", required=True)
    inbox_parser.add_argument("--agent", required=True)

    ack_parser = subparsers.add_parser("ack", help="按精确消息 ID 推进 Agent 游标")
    ack_parser.add_argument("--project", required=True)
    ack_parser.add_argument("--agent", required=True)
    ack_parser.add_argument("--message-id", required=True)

    task_parser = subparsers.add_parser("task-add", help="追加任务")
    task_parser.add_argument("--project", required=True)
    task_parser.add_argument("--title", required=True)
    task_parser.add_argument("--owner", required=True)

    route_parser = subparsers.add_parser("routing-add", help="追加路由展示状态（不会调用 Skill）")
    route_parser.add_argument("--project", required=True)
    route_parser.add_argument("--state", choices=tuple(ROUTING_STATES), required=True)
    route_parser.add_argument("--request-id", required=True)
    route_parser.add_argument("--skill-id", action="append", default=[])
    route_parser.add_argument("--agent-id")
    route_parser.add_argument("--note", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = PanelStore(args.project)
        if args.command == "init":
            agents = None if args.agent is None else [_parse_agent_cli(item) for item in args.agent]
            _print_json(
                store.initialize(
                    project_name=args.name,
                    user_name=args.user_name,
                    agents=agents,
                    max_agent_seats=args.max_agent_seats,
                    port=args.port,
                )
            )
        elif args.command == "serve":
            config = store._load_config()
            port = config["port"] if args.port is None else args.port
            server = make_server(store, port=port)
            actual_port = server.server_address[1]
            print(f"多 Agent 协作面板：http://{LOOPBACK_HOST}:{actual_port}/", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command == "snapshot":
            _print_json(store.snapshot())
        elif args.command == "roster":
            _print_json(store.snapshot()["roster"])
        elif args.command == "validate":
            snapshot = store.snapshot()
            _print_json(
                {
                    "ok": True,
                    "state_dir": str(store.state_dir),
                    "project_id": snapshot["project"]["id"],
                    "roster_revision": snapshot["roster"]["revision"],
                    "event_chain_valid": True,
                }
            )
        elif args.command in {"add-seat", "agent-add"}:
            _print_json(
                store.add_agent(
                    seat_id=args.seat_id,
                    display_name=args.display_name,
                    role_label=args.role_label,
                    expected_revision=args.expected_revision,
                    reason=args.reason,
                )
            )
        elif args.command in {"retire-seat", "agent-disable"}:
            _print_json(
                store.disable_agent(
                    seat_id=args.seat_id,
                    expected_revision=args.expected_revision,
                    reason=args.reason,
                )
            )
        elif args.command in {"hide-seat", "show-seat"}:
            _print_json(
                store.set_visibility(
                    seat_id=args.seat_id,
                    visible=args.command == "show-seat",
                    expected_revision=args.expected_revision,
                    reason=args.reason,
                )
            )
        elif args.command == "restore-seat":
            _print_json(
                store.restore_agent(
                    seat_id=args.seat_id,
                    expected_revision=args.expected_revision,
                    reason=args.reason,
                )
            )
        elif args.command == "send":
            _print_json(store.send_message(speaker=args.speaker, audience=args.audience, body=args.body))
        elif args.command == "inbox":
            _print_json(store.inbox(args.agent))
        elif args.command == "ack":
            _print_json(store.acknowledge(seat_id=args.agent, message_id=args.message_id))
        elif args.command == "task-add":
            _print_json(store.add_task(title=args.title, owner=args.owner))
        elif args.command == "routing-add":
            _print_json(
                store.record_routing_status(
                    state=args.state,
                    request_id=args.request_id,
                    skill_ids=args.skill_id,
                    agent_id=args.agent_id,
                    note=args.note,
                )
            )
        return 0
    except PanelError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
