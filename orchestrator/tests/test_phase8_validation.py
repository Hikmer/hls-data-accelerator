from __future__ import annotations

import unittest

from local_server import _validation_from_resources, effective_validation_config


APP = {"type": "Microsoft.App/containerApps", "name": "cardioe2e-app", "tags": {"hls-workload": "cardiology-app"}}


class Phase8CompletionValidationTests(unittest.TestCase):
    """Completion callers pass effective_validation_config(saved config), as here."""

    def validate(self, azure: list[dict], config: dict) -> dict:
        return _validation_from_resources({"azure": azure, "fabric": [], "workspace": None}, deploy_config=effective_validation_config(config))

    def test_phase8_only_passes_without_a_fabric_workspace(self) -> None:
        result = self.validate([APP], {"phase8_only": True})
        self.assertTrue(result["passed"], [c for c in result["checks"] if c["status"] == "fail"])
        self.assertNotIn("Fabric workspace exists", [c["name"] for c in result["checks"]])

    def test_phase8_only_fails_when_the_app_is_missing(self) -> None:
        self.assertFalse(self.validate([], {"phase8_only": True})["passed"])

    def test_full_deploy_still_requires_the_fabric_workspace(self) -> None:
        result = self.validate([APP], {})
        self.assertFalse(result["passed"])
        self.assertIn("Fabric workspace exists", [c["name"] for c in result["checks"] if c["status"] == "fail"])


if __name__ == "__main__":
    unittest.main()
