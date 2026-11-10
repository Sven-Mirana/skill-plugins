#!/usr/bin/env python3
"""Regression tests for fail-closed audit input handling."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AuditMalformedManifestTests(unittest.TestCase):
    @staticmethod
    def tree_hash(files: dict[str, str]) -> str:
        digest = hashlib.sha256()
        for relative, file_digest in sorted(files.items()):
            digest.update(relative.encode("utf-8") + b"\0" + file_digest.encode("ascii") + b"\n")
        return "sha256:" + digest.hexdigest()

    def test_validation_array_fails_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sublation-audit-malformed-") as tmp:
            candidate = Path(tmp) / "candidates" / "malformed"
            candidate.mkdir(parents=True)
            (candidate / "SKILL.md").write_text(
                "---\nname: malformed\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            (candidate / "manifest.json").write_text(
                json.dumps({"schema_version": 3, "validation": []}) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("audit.py")), str(candidate), "--json"],
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            report = json.loads(completed.stdout)
            consistency = next(
                check
                for check in report["checks"]
                if check["name"] == "strict_manifest_consistency"
            )
            self.assertFalse(consistency["passed"])
            self.assertIn("validation must be an object", consistency["detail"])

    def test_stale_candidate_snapshot_fails_without_manifest_self_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sublation-audit-stale-") as tmp:
            candidate = Path(tmp) / "candidates" / "stale"
            candidate.mkdir(parents=True)
            skill = candidate / "SKILL.md"
            patch = candidate / "PATCH.diff"
            skill.write_text("---\nname: stale\ndescription: fixture\n---\nbefore\n", encoding="utf-8")
            patch.write_text("nonempty fixture patch\n", encoding="utf-8")
            files = {
                path.name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (skill, patch)
            }
            manifest = {
                "schema_version": 3,
                "candidate_id": "fixture/stale",
                "candidate_type": "tooling",
                "candidate": {
                    "path": str(candidate),
                    "tree_hash": self.tree_hash(files),
                    "files": files,
                },
                "scope": {"changes": ["SKILL.md"], "out_of_scope": []},
                "validation": {
                    "auditor_status": "conditional",
                    "cross_reviewed_by": "none",
                    "promotion_mode": "none",
                    "status": "draft",
                },
            }
            (candidate / "manifest.json").write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )
            skill.write_text("---\nname: stale\ndescription: fixture\n---\nafter\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("audit.py")), str(candidate), "--json"],
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            report = json.loads(completed.stdout)
            snapshot = next(
                check
                for check in report["checks"]
                if check["name"] == "strict_candidate_snapshot_current"
            )
            self.assertFalse(snapshot["passed"])
            self.assertIn("SKILL.md", snapshot["detail"])
            self.assertNotIn("manifest.json", snapshot["detail"])


if __name__ == "__main__":
    unittest.main()
