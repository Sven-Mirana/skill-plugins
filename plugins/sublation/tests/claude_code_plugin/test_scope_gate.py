"""Tests for the scope gate that fronts observe/candidate/audit in the plugin.

Standard library only; every input is synthetic and built in a temp directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "skills" / "sublation" / "scope_gate.py"
FIXTURE_SKILL = ROOT / "fixtures" / "skill-routing" / "profiles" / "code-audit"


def gate(*argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-B", str(GATE), *argv], text=True, capture_output=True, env=env)


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


class ScopeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="scope-gate-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.source = self.tmp / "project" / "code-audit"
        self.source.mkdir(parents=True)
        for item in FIXTURE_SKILL.iterdir():
            (self.source / item.name).write_bytes(item.read_bytes())
        self.ledger = self.tmp / "ledger"
        self.candidates = self.ledger / "candidates"

    def create(self, source: Path, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return gate(
            "candidate", "--source", str(source), "--candidate-root", str(root), "--",
            "code-audit", "--agent", "claude-code", "--candidate-id", "t1", *extra,
        )

    def assert_refused(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["gate"], "refused")
        self.assertIs(report["written"], False)
        self.assertIn(needle, report["reason"])

    # -- the two reviewed counterexamples -------------------------------------

    def test_symlinks_in_source_are_refused_and_nothing_is_copied(self) -> None:
        outside = self.tmp / "outside"
        (outside / "dir").mkdir(parents=True)
        (outside / "MARKER.txt").write_text("outside file marker\n", encoding="utf-8")
        (outside / "dir" / "MARKER.txt").write_text("outside dir marker\n", encoding="utf-8")
        for name, target in (("link-file.txt", outside / "MARKER.txt"), ("link-dir", outside / "dir")):
            with self.subTest(name=name):
                link = self.source / name
                link.symlink_to(target)
                self.assert_refused(self.create(self.source, self.candidates), "符号链接")
                self.assert_refused(
                    gate("observe", "--source", str(self.source), "--observations-root",
                         str(self.ledger / "observations"), "--", "code-audit"),
                    "符号链接",
                )
                self.assertFalse(self.ledger.exists())
                link.unlink()

    def test_write_root_inside_source_is_refused_and_source_is_unchanged(self) -> None:
        # Working directory == Skill directory, with an earlier .sublation/ in it.
        (self.source / ".sublation" / "observations").mkdir(parents=True)
        before = tree(self.source)
        inner = self.source / ".sublation" / "candidates"
        self.assert_refused(self.create(self.source, inner), "源目录之内")
        relative = subprocess.run(
            [sys.executable, "-B", str(GATE), "candidate", "--source", ".", "--candidate-root",
             ".sublation/candidates", "--", "code-audit", "--candidate-id", "t1"],
            text=True, capture_output=True, cwd=self.source,
        )
        self.assert_refused(relative, "源目录之内")
        self.assert_refused(
            gate("observe", "--source", str(self.source), "--observations-root",
                 str(self.source / ".sublation" / "observations"), "--", "code-audit"),
            "源目录之内",
        )
        self.assertEqual(tree(self.source), before)

    # -- other refusals --------------------------------------------------------

    def test_write_root_reaching_source_through_a_link_is_refused(self) -> None:
        self.ledger.mkdir()
        (self.ledger / "candidates").symlink_to(self.source, target_is_directory=True)
        result = self.create(self.source, self.ledger / "candidates")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(tree(self.source), sorted(p.name for p in FIXTURE_SKILL.iterdir()))

    def test_active_roots_and_misnamed_roots_are_refused(self) -> None:
        self.assert_refused(self.create(self.source, self.tmp / ".claude" / "skills" / "candidates"), "活动")
        self.assert_refused(self.create(self.source, self.tmp / "ledger" / "drafts"), "candidates")
        self.assert_refused(self.create(self.source, self.tmp / "project" / "candidates" / ".." / ".."), "..")
        self.assert_refused(self.create(self.source, self.tmp / "project"), "包含源目录")
        self.assertFalse(self.ledger.exists())

    def test_controlled_flags_cannot_be_passed_through(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        for flag in ("--source-path", "--candidate-root", "--candidate-r", "--source-path=x"):
            with self.subTest(flag=flag):
                extra = (flag,) if "=" in flag else (flag, str(elsewhere))
                self.assert_refused(self.create(self.source, self.candidates, *extra), "范围门设置")
        self.assertFalse(self.ledger.exists())
        self.assertFalse(elsewhere.exists())

    # -- the legitimate path ---------------------------------------------------

    def make_candidate(self) -> Path:
        result = self.create(self.source, self.candidates, "--observation", "synthetic")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate = self.candidates / "code-audit" / "t1"
        self.assertTrue((candidate / "manifest.json").is_file())
        return candidate

    def test_separate_roots_pass_observe_candidate_and_audit(self) -> None:
        observed = gate(
            "observe", "--source", str(self.source), "--observations-root", str(self.ledger / "observations"),
            "--", "code-audit", "--session", "synthetic", "--classification", "partial",
            "--reflection-type", "OPTIMIZATION", "--step", "s", "--evidence", "e", "--summary", "m",
        )
        self.assertEqual(observed.returncode, 0, observed.stdout + observed.stderr)
        self.assertTrue(Path(observed.stdout.strip()).is_file())
        candidate = self.make_candidate()
        self.assertEqual(tree(self.source), sorted(p.name for p in FIXTURE_SKILL.iterdir()))
        audited = gate("audit", "--candidate", str(candidate), "--approve-root", str(self.tmp), "--", "--json")
        self.assertEqual(audited.returncode, 1, audited.stdout + audited.stderr)  # fresh scaffold: conditional
        self.assertEqual(json.loads(audited.stdout)["auditor_status"], "conditional")
        self.assertEqual(gate("check", "--source", str(self.source), "--write-root", str(self.ledger)).returncode, 0)

    # -- audit scope -----------------------------------------------------------

    def test_audit_refuses_manifest_paths_outside_the_approved_roots(self) -> None:
        candidate = self.make_candidate()
        canary = self.tmp / "outside-canary"
        canary.mkdir()
        (canary / "CANARY.txt").write_text("canary\n", encoding="utf-8")
        manifest_path = candidate / "manifest.json"
        original = manifest_path.read_text(encoding="utf-8")
        for keys in (("source_skill", "path"), ("validation", "formal_post_promotion", "path"),
                     ("validation", "post_promotion_safety", "promotion_target_path")):
            with self.subTest(keys=keys):
                manifest = json.loads(original)
                node = manifest
                for key in keys[:-1]:
                    node = node.setdefault(key, {})
                node[keys[-1]] = str(canary)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                result = gate("audit", "--candidate", str(candidate), "--approve-root", str(self.ledger),
                              "--approve-root", str(self.source), "--", "--json")
                self.assert_refused(result, "不在任何 --approve-root 之内")
                self.assertNotIn("CANARY", result.stdout + result.stderr)

    def test_audit_refuses_links_in_candidate_or_declared_source(self) -> None:
        candidate = self.make_candidate()
        canary = self.tmp / "outside-canary"
        canary.mkdir()
        (canary / "CANARY.txt").write_text("canary\n", encoding="utf-8")
        (candidate / "peek").symlink_to(canary, target_is_directory=True)
        result = gate("audit", "--candidate", str(candidate), "--approve-root", str(self.tmp))
        self.assert_refused(result, "符号链接")
        (candidate / "peek").unlink()
        (self.source / "peek.txt").symlink_to(canary / "CANARY.txt")
        result = gate("audit", "--candidate", str(candidate), "--approve-root", str(self.tmp))
        self.assert_refused(result, "符号链接")
        self.assertNotIn("CANARY", result.stdout)

    def test_audit_refuses_unapproved_candidate_and_broad_roots(self) -> None:
        candidate = self.make_candidate()
        self.assert_refused(
            gate("audit", "--candidate", str(candidate), "--approve-root", str(self.source)), "候选目录"
        )
        self.assert_refused(
            gate("audit", "--candidate", str(candidate), "--approve-root", str(Path.home())), "批准根"
        )
        self.assert_refused(gate("audit", "--candidate", str(candidate), "--approve-root", "/"), "批准根")

    # -- second-review counterexamples -----------------------------------------

    def test_ancestor_symlink_cannot_redirect_writes(self) -> None:
        out = self.tmp / "out-of-scope"
        (out / "candidates").mkdir(parents=True)
        (self.tmp / "alias").symlink_to(out, target_is_directory=True)
        self.assert_refused(self.create(self.source, self.tmp / "alias" / "candidates"), "符号链接")
        self.assert_refused(
            gate("observe", "--source", str(self.source), "--observations-root",
                 str(self.tmp / "alias" / "candidates"), "--", "code-audit"),
            "符号链接",
        )
        self.assertEqual(tree(out), ["candidates"])
        # the same holds for a source or an approved root reached through a link
        (self.tmp / "src-alias").symlink_to(self.source.parent, target_is_directory=True)
        self.assert_refused(self.create(self.tmp / "src-alias" / "code-audit", self.candidates), "符号链接")
        candidate = self.make_candidate()
        self.assert_refused(
            gate("audit", "--candidate", str(candidate), "--approve-root", str(self.tmp / "src-alias")), "符号链接"
        )
        self.assert_refused(
            gate("review", "--candidate", str(self.tmp / "alias"), "--approve-root", str(self.tmp)), "符号链接"
        )

    def test_case_variants_do_not_slip_past_markers_or_overlap(self) -> None:
        active = self.tmp / ".claude" / "skills" / "candidates"
        active.mkdir(parents=True)
        spelled = self.tmp / ".CLAUDE" / "SKILLS" / "candidates"
        result = gate("check", "--source", str(self.source), "--write-root", str(spelled), "--candidates")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(tree(active), [])
        upper = self.source.parent / self.source.name.upper()
        self.assert_refused(self.create(self.source, upper / "candidates"), "源目录之内")
        self.assert_refused(self.create(upper, self.source / "candidates"), "")
        self.assertEqual(tree(self.source), sorted(p.name for p in FIXTURE_SKILL.iterdir()))

    def test_audit_accepts_no_path_carrying_flags(self) -> None:
        candidate = self.make_candidate()
        key_file = self.tmp.parent / f"{self.tmp.name}-outside.key"
        key_file.write_text("SYNTHETIC-KEY-MARKER\n", encoding="utf-8")
        self.addCleanup(key_file.unlink)
        for extra in (("--routing-attestation-key", str(key_file)), ("--routing-att", str(key_file)),
                      (f"--routing-attestation-key={key_file}",), ("--json", str(key_file))):
            with self.subTest(extra=extra):
                result = gate("audit", "--candidate", str(candidate), "--approve-root", str(self.tmp), "--", *extra)
                self.assert_refused(result, "audit 只接受")
                self.assertNotIn("SYNTHETIC-KEY-MARKER", result.stdout + result.stderr)

    def test_unapproved_candidate_is_refused_before_it_is_walked(self) -> None:
        stranger = self.tmp / "stranger"
        (stranger / "deep").mkdir(parents=True)
        (stranger / "deep" / "peek").symlink_to(self.tmp)  # a walk would report this link
        for command in ("audit", "review"):
            result = gate(command, "--candidate", str(stranger), "--approve-root", str(self.ledger.parent / "project"))
            self.assert_refused(result, "不在任何 --approve-root 之内")
            self.assertNotIn("peek", result.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unreadable_directories_fail_closed(self) -> None:
        locked = self.source / "locked"
        locked.mkdir()
        (locked / "f.txt").write_text("x", encoding="utf-8")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.assert_refused(self.create(self.source, self.candidates), "无法遍历")
        self.assertFalse(self.ledger.exists())

    def test_review_preflight_lists_only_verified_regular_files(self) -> None:
        candidate = self.make_candidate()
        passed = gate("review", "--candidate", str(candidate), "--approve-root", str(self.tmp))
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        report = json.loads(passed.stdout)
        self.assertIn("manifest.json", report["readable_files"])
        self.assertIn("PATCH.diff", report["readable_files"])
        outside = self.tmp.parent / f"{self.tmp.name}-outside-manifest.json"
        outside.write_text('{"marker": "OUTSIDE-MANIFEST-MARKER"}', encoding="utf-8")
        self.addCleanup(outside.unlink)
        (candidate / "manifest.json").unlink()
        (candidate / "manifest.json").symlink_to(outside)
        refused = gate("review", "--candidate", str(candidate), "--approve-root", str(self.tmp))
        self.assert_refused(refused, "符号链接")
        self.assertNotIn("OUTSIDE-MANIFEST-MARKER", refused.stdout + refused.stderr)
        self.assertNotIn("readable_files", refused.stdout)

    # -- third-review counterexamples ------------------------------------------

    def test_raw_dotdot_is_refused_before_normalisation(self) -> None:
        candidate = self.make_candidate()
        approved = self.tmp / "approved"
        (approved / "source").mkdir(parents=True)
        nested = self.tmp / "outside" / "nested"
        nested.mkdir(parents=True)
        (self.tmp / "outside" / "source").mkdir()
        (self.tmp / "outside" / "source" / "DOTDOT-CANARY.txt").write_text("c", encoding="utf-8")
        (approved / "alias").symlink_to(nested, target_is_directory=True)
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_skill"]["path"] = f"{approved}/alias/../source"  # audit.py would read outside/source
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        for command in ("audit", "review"):
            result = gate(command, "--candidate", str(candidate), "--approve-root", str(self.ledger),
                          "--approve-root", str(approved))
            self.assert_refused(result, "..")
            self.assertNotIn("DOTDOT-CANARY", result.stdout + result.stderr)
        self.assert_refused(self.create(self.source.parent / "x" / ".." / "code-audit", self.candidates), "..")
        self.assert_refused(self.create(self.source, self.ledger / "x" / ".." / "candidates"), "..")
        self.assert_refused(
            gate("audit", "--candidate", f"{candidate.parent}/x/../t1", "--approve-root", str(self.tmp)), ".."
        )
        self.assertFalse((self.ledger / "x").exists())

    def test_approval_containment_is_case_sensitive(self) -> None:
        candidate = self.make_candidate()
        variant = Path(str(self.ledger).replace("ledger", "LEDGER"))
        if not variant.exists():  # case-sensitive volume: make the look-alike neighbour real
            variant.mkdir()
        for command in ("audit", "review"):
            result = gate(command, "--candidate", str(candidate), "--approve-root", str(variant))
            self.assert_refused(result, "不在任何 --approve-root 之内")
        # a manifest path that differs from the approved root only by case is not authorised either
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_skill"]["path"] = str(self.source).replace("project", "PROJECT")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = gate("review", "--candidate", str(candidate), "--approve-root", str(self.ledger),
                      "--approve-root", str(self.source.parent))
        self.assert_refused(result, "manifest.source_skill.path")

    def test_candidate_identity_uses_the_filesystem(self) -> None:
        candidate = self.make_candidate()
        other = candidate.parent / "t2"
        other.mkdir()
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["candidate"]["path"] = str(other)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_refused(
            gate("review", "--candidate", str(candidate), "--approve-root", str(self.tmp)), "不一致"
        )


if __name__ == "__main__":
    unittest.main()
