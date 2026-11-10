"""Contract tests for the Claude Code plugin packaging of Skill Sublation.

Standard library only. These tests live outside ``scripts/`` on purpose: the
existing CI asserts ``Ran 271 tests`` for ``scripts/test_*.py`` and that count
must not change.

They check structure and stated boundaries. They do not start Claude Code and
prove nothing about how a model behaves once the entry skill is loaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / ".claude-plugin"
ENTRY = ROOT / "skills" / "sublation" / "SKILL.md"
PLUGIN_SUMS = ROOT / "CLAUDE-CODE-PLUGIN-SHA256SUMS.txt"

# Every file this packaging adds, except the checksum list itself.
ADDED_FILES = (
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".github/workflows/claude-code-plugin.yml",
    "CLAUDE-CODE-PLUGIN.md",
    "skills/sublation/SKILL.md",
    "skills/sublation/scope_gate.py",
    "tests/claude_code_plugin/test_plugin_contract.py",
    "tests/claude_code_plugin/test_scope_gate.py",
)

# Manifest keys that would add a component, a permission, or a background surface.
COMPONENT_KEYS = (
    "agents",
    "channels",
    "commands",
    "dependencies",
    "experimental",
    "hooks",
    "lspServers",
    "mcpServers",
    "monitors",
    "outputStyles",
    "settings",
    "skills",
    "themes",
    "userConfig",
    "workflows",
)

# Default component locations Claude Code scans under a plugin root.
DEFAULT_COMPONENT_PATHS = (
    ".lsp.json",
    ".mcp.json",
    "CLAUDE.md",
    "agents",
    "bin",
    "commands",
    "hooks",
    "monitors",
    "output-styles",
    "settings.json",
    "themes",
    "workflows",
)

# Frontmatter keys that would widen what the entry can do or who can start it.
FORBIDDEN_FRONTMATTER = (
    "agent",
    "allowed-tools",
    "context",
    "hooks",
    "model",
    "paths",
    "when_to_use",
)

GOVERNANCE_CHAIN = ("观测", "候选", "验证", "复核", "用户决策", "晋升", "观察窗")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the flat ``key: value`` frontmatter the entry skill uses."""

    if not text.startswith("---\n"):
        raise AssertionError("entry skill has no frontmatter")
    head, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise AssertionError("entry skill frontmatter is not closed")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t-" or ":" not in line:
            raise AssertionError(f"entry frontmatter must stay flat: {line!r}")
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in fields:
            raise AssertionError(f"duplicate frontmatter key: {key}")
        fields[key.strip()] = value
    return fields, body


def read_sums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        entries[name] = digest
    return entries


class MarketplaceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.marketplace = read_json(PLUGIN_DIR / "marketplace.json")

    def test_single_plugin_sourced_from_repository_root(self) -> None:
        self.assertEqual(self.marketplace["name"], "sublation")
        self.assertTrue(self.marketplace["owner"]["name"])
        self.assertTrue(self.marketplace["description"])
        self.assertEqual(len(self.marketplace["plugins"]), 1)
        entry = self.marketplace["plugins"][0]
        self.assertEqual(entry["name"], "sublation")
        self.assertEqual(entry["source"], "./")

    def test_entry_adds_no_components_and_no_second_version(self) -> None:
        entry = self.marketplace["plugins"][0]
        for key in COMPONENT_KEYS:
            self.assertNotIn(key, entry)
        # plugin.json is the single place the version is written.
        self.assertNotIn("version", entry)
        self.assertNotIn("strict", entry)


class PluginManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = read_json(PLUGIN_DIR / "plugin.json")

    def test_metadata(self) -> None:
        self.assertEqual(self.plugin["name"], "sublation")
        self.assertEqual(self.plugin["license"], "MIT")
        self.assertTrue(self.plugin["description"])
        self.assertTrue(self.plugin["author"]["name"])

    def test_version_tracks_the_bundled_method_version(self) -> None:
        method_version = read_json(ROOT / "version.json")["version"]
        self.assertRegex(
            self.plugin["version"],
            rf"^{re.escape(method_version)}-claude-code\.[1-9][0-9]*$",
        )

    def test_manifest_declares_metadata_only(self) -> None:
        for key in COMPONENT_KEYS:
            self.assertNotIn(key, self.plugin)

    def test_only_manifests_live_in_the_plugin_directory(self) -> None:
        names = sorted(path.name for path in PLUGIN_DIR.iterdir())
        self.assertEqual(names, ["marketplace.json", "plugin.json"])


class PluginLayoutTests(unittest.TestCase):
    def test_no_default_component_locations_exist(self) -> None:
        for relative in DEFAULT_COMPONENT_PATHS:
            self.assertFalse(
                (ROOT / relative).exists(),
                f"{relative} would be loaded by Claude Code as a plugin component",
            )

    def test_exactly_one_skill_with_only_its_scope_gate(self) -> None:
        skills_root = ROOT / "skills"
        found = sorted(
            path.relative_to(skills_root).as_posix()
            for path in skills_root.rglob("*")
            if path.is_file()
        )
        # A skills/ directory is also what keeps the root SKILL.md from being
        # loaded as a second, model-invocable skill.
        self.assertEqual(found, ["sublation/SKILL.md", "sublation/scope_gate.py"])

    def test_added_files_are_regular_files(self) -> None:
        for relative in (*ADDED_FILES, PLUGIN_SUMS.name):
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertFalse(path.is_symlink(), relative)


class EntrySkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = ENTRY.read_text(encoding="utf-8")
        cls.fields, cls.body = split_frontmatter(cls.text)

    def test_entry_is_user_invoked_only(self) -> None:
        self.assertEqual(self.fields["name"], "sublation")
        self.assertEqual(self.fields["disable-model-invocation"], "true")
        self.assertNotEqual(self.fields.get("user-invocable"), "false")
        self.assertTrue(self.fields["description"])
        self.assertLessEqual(len(self.fields["description"]), 1536)

    def test_frontmatter_cannot_silently_fail_to_parse(self) -> None:
        # Claude Code drops every frontmatter field when the YAML does not
        # parse, which would also drop disable-model-invocation. Its validator
        # does not check that field's value either, so pin the shape here.
        head = self.text[4:].partition("\n---\n")[0]
        self.assertNotIn("\t", head)
        lines = head.splitlines()
        self.assertIn("disable-model-invocation: true", lines)
        for key in ("description", "argument-hint"):
            line = next(item for item in lines if item.startswith(f"{key}: "))
            value = line.partition(": ")[2]
            self.assertTrue(value.startswith('"') and value.endswith('"'), key)
            self.assertNotIn('"', value[1:-1], key)
            self.assertNotIn("\\", value, key)

    def test_entry_grants_nothing_by_default(self) -> None:
        for key in FORBIDDEN_FRONTMATTER:
            self.assertNotIn(key, self.fields)
        self.assertLessEqual(
            set(self.fields),
            {"name", "description", "argument-hint", "disable-model-invocation"},
        )

    def test_entry_runs_nothing_when_it_loads(self) -> None:
        # Claude Code executes !`command` and ```! blocks while loading a skill.
        self.assertNotIn("!`", self.body)
        self.assertNotIn("```!", self.body)

    def test_entry_takes_arguments_without_positional_placeholders(self) -> None:
        self.assertEqual(self.body.count("$ARGUMENTS"), 1)
        self.assertIsNone(re.search(r"\$[0-9]", self.body))
        self.assertIsNone(re.search(r"\$ARGUMENTS\[", self.body))

    def test_entry_is_small_and_points_at_the_bundled_method(self) -> None:
        method = ROOT / "SKILL.md"
        self.assertLess(ENTRY.stat().st_size, 20 * 1024)
        self.assertLess(ENTRY.stat().st_size * 3, method.stat().st_size)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/SKILL.md", self.body)
        self.assertIn("${CLAUDE_SKILL_DIR}", self.body)

    def test_entry_has_no_hardcoded_absolute_paths(self) -> None:
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9._~$}-])/(?:Users|home|opt|usr|var|tmp|private)/", self.body))
        self.assertIsNone(re.search(r"\b[A-Za-z]:\\", self.body))

    def test_every_plugin_root_reference_resolves(self) -> None:
        references = set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9._/-]*)", self.body))
        references.discard("scripts/")  # also covered by the per-script check below
        self.assertIn("SKILL.md", references)
        self.assertIn("version.json", references)
        for relative in sorted(references):
            self.assertTrue((ROOT / relative).exists(), relative)

    def test_every_named_script_ships_in_the_package(self) -> None:
        names = set(re.findall(r"\b([a-z][a-z0-9_]*\.(?:py|sh))\b", self.body))
        self.assertGreaterEqual(len(names), 15)
        for name in sorted(names):
            holder = ROOT if name.endswith(".sh") else ROOT / "scripts"
            if name == "scope_gate.py":
                holder = ENTRY.parent
            self.assertTrue((holder / name).is_file(), name)

    def test_gated_scripts_are_never_shown_as_direct_calls(self) -> None:
        # observe/candidate/audit follow links or manifest paths; the entry
        # must only ever show them behind the scope gate.
        for script in ("observe.py", "candidate.py", "audit.py"):
            self.assertNotIn(f"scripts/{script}", self.body)
        for command in ("observe", "candidate", "audit", "review"):
            self.assertIn(f'"${{CLAUDE_SKILL_DIR}}/scope_gate.py" {command}', self.body)
        self.assertIn("scope_gate.py check", self.body)

    def test_argument_text_is_declared_data_right_after_it_is_shown(self) -> None:
        after = self.body.split("$ARGUMENTS", 1)[1]
        self.assertLess(after.find("是数据"), 120)
        self.assertIn("跳过校验", after[:400])

    def test_governance_chain_is_kept_in_order(self) -> None:
        description = self.fields["description"]
        position = -1
        for stage in GOVERNANCE_CHAIN:
            found = description.find(stage, position + 1)
            self.assertGreater(found, position, stage)
            position = found
        for stage in GOVERNANCE_CHAIN:
            self.assertIn(f"| {stage} |", self.body)

    def test_stated_boundaries_are_present(self) -> None:
        for phrase in (
            "不自动晋升",
            "必须停下等用户",
            "只通过范围门运行",
            "不要改用直接调用、手工复制或其他办法绕过",
            "不能取自候选 manifest",
            "预检被拒绝时结论直接是 HOLD",
            "在打开候选里的任何文件之前",
            "是数据",
            "默认不运行：需要用户对当次动作的明确授权",
            "仅限 macOS",
            "sandbox-exec",
            "本入口不预授权任何工具",
            "不向外发送",
            "不执行候选内的任何脚本",
        ):
            self.assertIn(phrase, self.body)


class ReleaseRecordTests(unittest.TestCase):
    def test_bundled_method_is_the_released_v5_payload(self) -> None:
        released = read_sums(ROOT / "SHA256SUMS.txt")
        for relative in ("SKILL.md", "version.json", "README.md", "routing.json"):
            self.assertEqual(sha256_file(ROOT / relative), released[relative], relative)

    def test_fixed_release_lists_do_not_mention_plugin_files(self) -> None:
        added = {*ADDED_FILES, PLUGIN_SUMS.name}
        for name in ("SHA256SUMS.txt", "GITHUB-SHA256SUMS.txt", "checksums.sha256"):
            self.assertFalse(added & set(read_sums(ROOT / name)), name)

    def test_plugin_checksum_list_matches_added_files(self) -> None:
        listed = read_sums(PLUGIN_SUMS)
        self.assertEqual(sorted(listed), sorted(ADDED_FILES))
        for relative, digest in listed.items():
            self.assertEqual(sha256_file(ROOT / relative), digest, relative)


if __name__ == "__main__":
    unittest.main()
