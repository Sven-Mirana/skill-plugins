#!/usr/bin/env python3
"""Scope gate for the Claude Code entry of Skill Sublation.

The bundled V5.0.0 CLIs trust the paths they are given: ``candidate.py``
copies a source tree with symlinks followed, ``observe.py`` hashes it the same
way, and ``audit.py`` reads whatever directories a candidate manifest names.
This gate checks the real, resolved locations first and only then runs the
unmodified bundled script. A refusal exits 2 before anything is written.

Checked before any bundled script starts:

* every path the caller or a manifest supplies is walked component by
  component; a symlink anywhere in it refuses (only the macOS aliases /tmp,
  /var and /etc are rewritten to /private/...);
* a raw ".." component refuses before any normalisation;
* overlap and active-location checks fold case and Unicode form; containment
  in an approved root is exact and case-sensitive, and candidate identity is
  decided by the filesystem (samefile);
* the source Skill is a real directory with no symlink or special file inside;
* the write root does not overlap the source, this plugin, or an active Skill
  or plugin location;
* for review and audit, the candidate must lie inside a user-approved root
  before anything below it is traversed, and every directory its manifest
  declares must lie inside an approved root too;
* audit accepts only --json, --strict and --no-strict after ``--``.

The gate cannot see what happens between its check and a later read (a tree
changed in that window is not detected), and it only guards calls that
actually go through it.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

PLUGIN_ROOT = Path(os.path.realpath(__file__)).parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
REFUSED = 2

# Directory pairs that mark an active Skill or plugin location.
ACTIVE_MARKERS = (
    (".claude", "skills"),
    (".claude", "plugins"),
    (".claude", "commands"),
    (".codex", "skills"),
    (".hermes", "skills"),
    (".agents", "skills"),
)
# Flags the gate sets itself; callers may not pass them through.
CONTROLLED_FLAGS = {
    "observe": ("--skill-path", "--observations-root"),
    "candidate": ("--source-path", "--candidate-root"),
    "audit": (),
}
MANIFEST_PATHS = (
    ("source_skill", "path"),
    ("candidate", "path"),
    ("validation", "formal_post_promotion", "path"),
    ("validation", "post_promotion_safety", "promotion_target_path"),
)
MAX_ENTRIES = 20000


class Refusal(Exception):
    """A scope rule failed; nothing may be written or read further."""


# macOS ships these three aliases into /private. They are the only symlinks a
# user path may pass through, and they are rewritten to the physical form.
SYSTEM_ALIASES = {"/tmp": "/private/tmp", "/var": "/private/var", "/etc": "/private/etc"}
AUDIT_FLAGS = ("--json", "--strict", "--no-strict")


def key(path: Path) -> tuple[str, ...]:
    """Comparison key: case- and Unicode-form-insensitive on every platform.

    macOS volumes are normally case-insensitive, so two spellings can name one
    directory. Folding everywhere only ever makes the gate refuse more.
    """

    return tuple(unicodedata.normalize("NFD", part).casefold() for part in path.parts)


def inside(path: Path, root: Path) -> bool:
    child, parent = key(path), key(root)
    return child[: len(parent)] == parent


def within(path: Path, root: Path) -> bool:
    """Positive containment used to *grant* scope: exact, case-sensitive parts.

    Folding is right for refusing overlap but would authorise a sibling that
    differs only by case on a case-sensitive volume. On a case-insensitive
    volume this asks for the same spelling as the approved root.
    """

    return path.parts[: len(root.parts)] == root.parts


def same_dir(left: Path, right: Path) -> bool:
    """Identity by what the filesystem says, falling back to exact spelling."""

    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.parts == right.parts


def same(left: Path, right: Path) -> bool:
    return key(left) == key(right)


def physical(raw: object, label: str) -> Path:
    """Absolute path with every existing component checked: no symlink allowed."""

    if not isinstance(raw, str) or not raw.strip():
        raise Refusal(f"{label}: 路径为空或不是字符串")
    if "\0" in raw:
        raise Refusal(f"{label}: 路径含 NUL")
    # Refuse ".." before any lexical normalisation: the bundled scripts resolve
    # the raw string, where "link/.." can land somewhere abspath would not.
    if ".." in Path(raw).parts:
        raise Refusal(f"{label}: 路径含 .. 组件，请给出不含 .. 的物理路径")
    text = os.path.abspath(os.path.expanduser(raw))
    if sys.platform == "darwin":
        for alias, target in SYSTEM_ALIASES.items():
            if text == alias or text.startswith(alias + "/"):
                text = target + text[len(alias):]
                break
    path = Path(text)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as error:
            raise Refusal(f"{label}: 无法检查路径 {current}: {error.strerror}") from error
        if stat.S_ISLNK(mode):
            raise Refusal(f"{label}: 路径经过符号链接 {current}；请改用物理路径")
    try:
        resolved = Path(os.path.realpath(text))
    except OSError as error:
        raise Refusal(f"{label}: 无法解析路径: {error.strerror}") from error
    if not same(resolved, path):
        raise Refusal(f"{label}: 解析结果与给定路径不一致: {resolved}")
    return path


def physical_dir(raw: object, label: str) -> Path:
    path = physical(raw, label)
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise Refusal(f"{label}: 不是已存在的目录: {path}") from error
    if not stat.S_ISDIR(mode):
        raise Refusal(f"{label}: 不是已存在的目录: {path}")
    return path


def too_broad(path: Path) -> bool:
    return same(path, Path(path.anchor)) or same(path, Path(os.path.realpath(Path.home())))


def active_marker(path: Path) -> str | None:
    parts = key(path)
    for first, second in ACTIVE_MARKERS:
        for index in range(len(parts) - 1):
            if parts[index] == first and parts[index + 1] == second:
                return f"{first}/{second}"
    return None


def scan_tree(root: Path, label: str) -> list[str]:
    """Refuse any symlink or special file below ``root``; never follow links.

    Returns the relative paths of the regular files. Any read error refuses.
    """

    def fail(error: OSError) -> None:
        raise Refusal(f"{label}: 无法遍历 {error.filename}: {error.strerror}")

    files: list[str] = []
    count = 0
    for current, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs.sort()
        for name in dirs + sorted(names):
            entry = Path(current) / name
            try:
                mode = entry.lstat().st_mode
            except OSError as error:
                raise Refusal(f"{label}: 无法检查 {entry}: {error.strerror}") from error
            relative = entry.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                raise Refusal(f"{label}: 目录内含符号链接，拒绝读取或复制: {relative}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise Refusal(f"{label}: 目录内含非常规文件: {relative}")
            if stat.S_ISREG(mode):
                files.append(relative)
            count += 1
            if count > MAX_ENTRIES:
                raise Refusal(f"{label}: 条目超过 {MAX_ENTRIES}，不像单个 Skill 目录")
    return sorted(files)


def check_source(raw: str) -> Path:
    source = physical_dir(raw, "源目录")
    if too_broad(source):
        raise Refusal("源目录: 不能是文件系统根或用户主目录")
    scan_tree(source, "源目录")
    return source


def check_write_root(raw: str, source: Path, *, need_candidates: bool) -> Path:
    root = physical(raw, "写入根")
    if inside(root, source):
        raise Refusal(f"写入根: 位于源目录之内，会把输出写回被治理的 Skill: {root}")
    if inside(source, root):
        raise Refusal(f"写入根: 包含源目录: {root}")
    if inside(root, PLUGIN_ROOT) or inside(PLUGIN_ROOT, root):
        raise Refusal("写入根: 与本插件安装目录重叠")
    marker = active_marker(root)
    if marker:
        raise Refusal(f"写入根: 位于活动 Skill 或插件位置 ({marker}) 之内")
    if need_candidates and "candidates" not in root.parts:
        raise Refusal("写入根: 候选根的路径里必须有名为 candidates 的目录（小写）")
    probe = root
    while not os.path.lexists(probe):
        probe = probe.parent
    if not stat.S_ISDIR(probe.lstat().st_mode):
        raise Refusal(f"写入根: 已存在但不是目录: {probe}")
    if os.path.lexists(root):
        scan_tree(root, "写入根")
    return root


def check_passthrough(command: str, extra: list[str]) -> list[str]:
    extra = extra[1:] if extra[:1] == ["--"] else extra
    if command == "audit":
        for token in extra:
            if token not in AUDIT_FLAGS:
                raise Refusal(f"audit 只接受 {'、'.join(AUDIT_FLAGS)}，不接受 {token.split('=', 1)[0]}")
        return extra
    for token in extra:
        if not token.startswith("--"):
            continue
        name = token.split("=", 1)[0]
        if len(name) > 2 and any(flag.startswith(name) for flag in CONTROLLED_FLAGS[command]):
            raise Refusal(f"参数 {name} 由范围门设置，不能在 -- 之后再传")
    return extra


def verify_candidate(raw_candidate: str, raw_roots: list[str]) -> tuple[Path, list[str], dict[str, str]]:
    """Scope first, traversal second: nothing below an unapproved path is walked."""

    approved = [physical_dir(raw, "批准根") for raw in raw_roots]
    for root in approved:
        if too_broad(root):
            raise Refusal("批准根: 不能是文件系统根或用户主目录")
    candidate = physical_dir(raw_candidate, "候选目录")
    if not any(within(candidate, root) for root in approved):
        raise Refusal("候选目录: 不在任何 --approve-root 之内")
    if active_marker(candidate):
        raise Refusal("候选目录: 位于活动 Skill 或插件位置之内")
    files = scan_tree(candidate, "候选目录")
    if "manifest.json" not in files:
        raise Refusal("候选目录: 缺少 manifest.json（常规文件）")
    try:
        manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"manifest.json 无法读取或解析: {error}") from error
    if not isinstance(manifest, dict):
        raise Refusal("manifest.json 不是对象")
    declared: dict[str, str] = {}
    for keys in MANIFEST_PATHS:
        value: object = manifest
        for name in keys:
            value = value.get(name) if isinstance(value, dict) else None
        label = "manifest." + ".".join(keys)
        if value in (None, ""):
            continue
        path = physical(value, label)
        if not any(within(path, root) for root in approved):
            raise Refusal(f"{label}: 不在任何 --approve-root 之内: {path}")
        if keys == ("candidate", "path") and not same_dir(path, candidate):
            raise Refusal(f"{label}: 与被审计的候选目录不一致")
        if os.path.lexists(path):
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise Refusal(f"{label}: 不是目录: {path}")
            scan_tree(path, label)
        declared[label] = str(path)
    return candidate, files, declared


def run_bundled(script: str, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *argv], env=env).returncode


def cmd_check(args: argparse.Namespace) -> int:
    source = check_source(args.source)
    write_root = check_write_root(args.write_root, source, need_candidates=args.candidates)
    print(json.dumps({"gate": "pass", "source": str(source), "write_root": str(write_root)}, ensure_ascii=False))
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    extra = check_passthrough(args.command, args.rest)
    source = check_source(args.source)
    root = check_write_root(args.observations_root, source, need_candidates=False)
    return run_bundled("observe.py", [*extra, "--skill-path", str(source), "--observations-root", str(root)])


def cmd_candidate(args: argparse.Namespace) -> int:
    extra = check_passthrough(args.command, args.rest)
    source = check_source(args.source)
    root = check_write_root(args.candidate_root, source, need_candidates=True)
    return run_bundled(
        "candidate.py", ["create", *extra, "--source-path", str(source), "--candidate-root", str(root)]
    )


def cmd_review(args: argparse.Namespace) -> int:
    candidate, files, declared = verify_candidate(args.candidate, args.approve_root)
    print(json.dumps(
        {"gate": "pass", "candidate": str(candidate), "readable_files": files, "declared_paths": declared},
        ensure_ascii=False, indent=2,
    ))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    extra = check_passthrough(args.command, args.rest)
    candidate, _files, _declared = verify_candidate(args.candidate, args.approve_root)
    return run_bundled(
        "audit.py", [str(candidate), *extra], {"SUBLATION_RUNTIME_AGENT": "claude-code"}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sublation Claude Code 入口的范围门", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", allow_abbrev=False, help="只校验，不运行任何脚本")
    check.add_argument("--source", required=True)
    check.add_argument("--write-root", required=True)
    check.add_argument("--candidates", action="store_true", help="按候选根的规则校验写入根")
    check.set_defaults(func=cmd_check)

    observe = sub.add_parser("observe", allow_abbrev=False, help="校验后运行 observe.py")
    observe.add_argument("--source", required=True)
    observe.add_argument("--observations-root", required=True)
    observe.add_argument("rest", nargs=argparse.REMAINDER)
    observe.set_defaults(func=cmd_observe)

    candidate = sub.add_parser("candidate", allow_abbrev=False, help="校验后运行 candidate.py create")
    candidate.add_argument("--source", required=True)
    candidate.add_argument("--candidate-root", required=True)
    candidate.add_argument("rest", nargs=argparse.REMAINDER)
    candidate.set_defaults(func=cmd_candidate)

    audit = sub.add_parser("audit", allow_abbrev=False, help="校验 manifest 声明的路径后运行 audit.py")
    audit.add_argument("--candidate", required=True)
    audit.add_argument("--approve-root", action="append", required=True)
    audit.add_argument("rest", nargs=argparse.REMAINDER)
    audit.set_defaults(func=cmd_audit)

    review = sub.add_parser("review", allow_abbrev=False, help="静态评审前的预检：只列出可读文件，不运行脚本")
    review.add_argument("--candidate", required=True)
    review.add_argument("--approve-root", action="append", required=True)
    review.set_defaults(func=cmd_review)

    args = parser.parse_args(argv)
    try:
        try:
            return args.func(args)
        except OSError as error:  # anything unreadable fails closed
            raise Refusal(f"文件系统错误: {error.strerror or error}") from error
    except Refusal as refusal:
        print(json.dumps({"gate": "refused", "reason": str(refusal), "written": False}, ensure_ascii=False))
        print("范围门拒绝：未运行任何脚本，未写入任何文件。请修正路径或向用户确认后重试。", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
