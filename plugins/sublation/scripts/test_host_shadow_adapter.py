#!/usr/bin/env python3
"""Tests for the default-off, advisory-only host-shadow adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import host_shadow_adapter
import skill_router


ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "validation" / "skill-routing-index.json"
FIXTURE_PATH = ROOT / "fixtures" / "skill-routing" / "cases.json"
PROFILES_PATH = ROOT / "fixtures" / "skill-routing" / "profiles"


class HostShadowAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.taxonomy = skill_router.load_taxonomy()
        cls.enabled = {**host_shadow_adapter.load_config(), "enabled": True}

    def packet(self, **extra: object) -> dict:
        return {
            "schema_version": host_shadow_adapter.PACKET_SCHEMA_VERSION,
            "request_id": "opaque-001",
            "sample_source": "redacted_features",
            "features": {},
            **extra,
        }

    def test_default_config_is_disabled_and_fail_closed(self) -> None:
        config = host_shadow_adapter.load_config()
        self.assertFalse(config["enabled"])
        self.assertFalse(config["allow_automatic_execution"])
        self.assertFalse(config["allow_live_routing"])

    def test_disabled_path_does_not_parse_packet_and_preserves_selection(self) -> None:
        baseline = ["host:unchanged"]
        result = host_shadow_adapter.observe(
            {"prompt": "must-not-be-read"}, baseline, self.index, self.taxonomy
        )
        self.assertEqual(result["status"], "disabled_noop")
        self.assertEqual(result["effective_selection"], baseline)
        self.assertIsNone(result["shadow_advice"])
        self.assertEqual(result["overhead"]["input_bytes"], 0)

    def test_enabled_path_rejects_raw_prompt(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            host_shadow_adapter.observe(
                {**self.packet(), "prompt": "raw"},
                ["host:selection"],
                self.index,
                self.taxonomy,
                config=self.enabled,
            )

    def test_unknown_feature_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown or unreviewed"):
            host_shadow_adapter.observe(
                self.packet(features={"positive_triggers": ["trigger:not-reviewed:0"]}),
                ["host:selection"],
                self.index,
                self.taxonomy,
                config=self.enabled,
            )

    def test_closed_vocabulary_feature_returns_advice_without_changing_host(self) -> None:
        catalog = host_shadow_adapter._feature_catalog(self.index, self.taxonomy)
        feature_id = next(key for key in catalog if key.startswith("skill:fixture:document-format"))
        baseline = ["host:document-original"]
        result = host_shadow_adapter.observe(
            self.packet(features={"explicit_skills": [feature_id]}),
            baseline,
            self.index,
            self.taxonomy,
            config=self.enabled,
            runtime_roots=[("fixture", PROFILES_PATH)],
        )
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["effective_selection"], baseline)
        self.assertEqual(result["shadow_advice"]["skill"], "document-format")
        self.assertGreater(result["shadow_advice"]["confidence"], 0)
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["authority_granted"])

    def test_feature_id_in_wrong_group_is_rejected(self) -> None:
        catalog = host_shadow_adapter._feature_catalog(self.index, self.taxonomy)
        feature_id = next(key for key in catalog if key.startswith("skill:"))
        with self.assertRaisesRegex(ValueError, "wrong group"):
            host_shadow_adapter.observe(
                self.packet(features={"domains": [feature_id]}),
                ["host:selection"],
                self.index,
                self.taxonomy,
                config=self.enabled,
            )

    def test_only_synthetic_fixture_source_is_allowed_for_case_ids(self) -> None:
        changed = {**self.fixture, "sample_source": "authorized_shadow_runtime"}
        packet = {
            "schema_version": host_shadow_adapter.PACKET_SCHEMA_VERSION,
            "request_id": "synthetic-single-01",
            "sample_source": "synthetic_fixture",
            "fixture_case_id": "single-01",
        }
        with self.assertRaisesRegex(ValueError, "frozen synthetic fixture"):
            host_shadow_adapter.observe(
                packet,
                ["host:selection"],
                self.index,
                self.taxonomy,
                config=self.enabled,
                fixture=changed,
            )

    def test_synthetic_case_output_does_not_store_prompt(self) -> None:
        packet = {
            "schema_version": host_shadow_adapter.PACKET_SCHEMA_VERSION,
            "request_id": "synthetic-single-01",
            "sample_source": "synthetic_fixture",
            "fixture_case_id": "single-01",
        }
        result = host_shadow_adapter.observe(
            packet,
            ["host:selection"],
            self.index,
            self.taxonomy,
            config=self.enabled,
            fixture=self.fixture,
            runtime_roots=[("fixture", PROFILES_PATH)],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.fixture["cases"][0]["prompt"], serialized)
        self.assertFalse(result["raw_prompt_accepted"])

    def test_all_39_synthetic_cases_preserve_host_and_pass(self) -> None:
        report = host_shadow_adapter.evaluate_synthetic(
            self.fixture,
            self.index,
            self.taxonomy,
            [("fixture", PROFILES_PATH)],
        )
        self.assertEqual(report["total"], 39)
        self.assertEqual(report["passed"], 39)
        self.assertEqual(report["failed"], 0)
        self.assertTrue(report["host_selection_preserved"])
        self.assertEqual(report["net_token_savings_status"], "not_measurable")
        self.assertFalse(report["host_deferred_loading_verified"])

    def test_config_cannot_enable_execution_or_live_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        **host_shadow_adapter.load_config(),
                        "enabled": True,
                        "allow_automatic_execution": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "allow_automatic_execution"):
                host_shadow_adapter.load_config(path)


if __name__ == "__main__":
    unittest.main()
