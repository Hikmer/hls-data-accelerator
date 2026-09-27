from __future__ import annotations

import unittest

from activities.invoke_powershell import _build_deploy_args


# A local checkout path that exercises both shell-quoting hazards at once.
APP_PATH = "/home/o'brien/cardio apps/caldova-cardio-e2e"
APP_USERS = ["one@example.test", "two@example.test"]

BASE_CONFIG = {
    "fabric_workspace_name": "med-test",
    "resource_group_name": "rg-test",
}


class Phase8DeployArgumentTests(unittest.TestCase):
    """Every launch carries the policy tag, so Deploy-All always runs via -Command."""

    def command(self, **overrides) -> str:
        args = _build_deploy_args({**BASE_CONFIG, **overrides})
        self.assertEqual(args[3], "-Command")
        return args[4]

    def test_phase8_switches_follow_config(self) -> None:
        self.assertIn(" -Phase8", self.command(phase8_only=True))
        self.assertIn(" -SkipCardiologyApp", self.command(skip_cardiology_app=True))
        default = self.command()
        self.assertNotIn("-Phase8", default)
        self.assertNotIn("-SkipCardiologyApp", default)

    def test_cardiology_app_path_survives_spaces_and_quotes(self) -> None:
        self.assertIn(
            "-CardiologyAppPath '/home/o''brien/cardio apps/caldova-cardio-e2e'",
            self.command(cardiology_app_path=APP_PATH),
        )

    def test_cardiology_app_users_become_a_powershell_array(self) -> None:
        self.assertIn(
            "-CardiologyAppUsers @('one@example.test','two@example.test')",
            self.command(cardiology_app_users=APP_USERS),
        )

    def test_empty_cardiology_inputs_emit_no_parameters(self) -> None:
        self.assertNotIn("-CardiologyApp", self.command(cardiology_app_path="", cardiology_app_users=[]))


if __name__ == "__main__":
    unittest.main()
