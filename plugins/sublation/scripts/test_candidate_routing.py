#!/usr/bin/env python3
"""Routing metadata integration tests for candidate.py."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

import candidate
import skill_router


class CandidateRoutingTests(unittest.TestCase):
    def test_new_candidate_gets_non_authoritative_routing_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text(
                "---\nname: legal-helper\ndescription: 审查法律合同并提取风险\n---\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                skill_name="legal-helper",
                source_path=str(source),
                candidate_root=str(root / "candidates"),
                candidate_type="tooling",
                agent="codex",
                candidate_id="candidate-1",
                observation=["synthetic"],
            )
            self.assertEqual(candidate.create(args), 0)
            candidate_root = root / "candidates" / "legal-helper" / "candidate-1"
            routing = json.loads((candidate_root / "routing.json").read_text(encoding="utf-8"))
            manifest = json.loads((candidate_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(routing["classification"]["status"], "suggested")
            self.assertEqual(routing["primary_domain"], "legal")
            self.assertEqual(manifest["routing"]["classification_status"], "suggested")
            self.assertEqual(
                manifest["routing"]["metadata_sha256"],
                skill_router.metadata_sha256(routing),
            )

    def test_invalid_existing_routing_metadata_aborts_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text(
                "---\nname: invalid-route\ndescription: invalid fixture\n---\n",
                encoding="utf-8",
            )
            (source / "routing.json").write_text("{}\n", encoding="utf-8")
            args = argparse.Namespace(
                skill_name="invalid-route",
                source_path=str(source),
                candidate_root=str(root / "candidates"),
                candidate_type="tooling",
                agent="codex",
                candidate_id="candidate-1",
                observation=[],
            )
            with self.assertRaises(SystemExit):
                candidate.create(args)
            self.assertFalse((root / "candidates" / "invalid-route" / "candidate-1").exists())


if __name__ == "__main__":
    unittest.main()
