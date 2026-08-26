from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_architecture import Rules, check_source, load_rules
from scripts.check_governance import _check_greptile, check_repository


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ArchitectureGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(REPOSITORY_ROOT)

    def inspect(self, relative_path: str, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / self.rules.source_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            return [violation.message for violation in check_source(root, self.rules)]

    def assert_rejected(self, relative_path: str, source: str, expected: str) -> None:
        messages = self.inspect(relative_path, source)
        self.assertTrue(
            any(expected in message for message in messages),
            f"expected {expected!r} in {messages!r}",
        )

    def test_approved_dependency_is_accepted(self) -> None:
        messages = self.inspect(
            "core/agent_loop.py", "from alx.contracts.goal import Goal\n"
        )
        self.assertEqual([], messages)

    def test_tool_cannot_import_core(self) -> None:
        self.assert_rejected(
            "tools/mail.py",
            "from alx.core.agent_loop import AgentLoop\n",
            "tools may not import core",
        )

    def test_model_sdk_is_provider_only(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "from xai_sdk import Client\n",
            "allowed only in providers",
        )

    def test_raw_language_is_rejected_at_tool_boundary(self) -> None:
        self.assert_rejected(
            "tools/mail.py",
            "def search(user_text: str) -> list[str]:\n    return []\n",
            "raw-language parameter",
        )

    def test_phrase_comparison_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            'def choose(message: str) -> str:\n    if message.lower() == "send email":\n        return "mail"\n    return ""\n',
            "raw-language comparison",
        )

    def test_fixed_text_action_map_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            'actions = {"send email": "mail", "check diary": "calendar"}\n',
            "fixed text-to-action mapping",
        )

    def test_relative_import_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "from ..contracts import Goal\n",
            "relative imports are prohibited",
        )

    def test_workflow_source_name_is_rejected(self) -> None:
        self.assert_rejected(
            "tools/invoice_workflow.py",
            "VALUE = 1\n",
            "forbidden routing/workflow source name",
        )


class GovernanceGateTests(unittest.TestCase):
    def test_repository_governance_passes(self) -> None:
        self.assertEqual([], check_repository(REPOSITORY_ROOT))

    def test_greptile_constitutional_config_passes(self) -> None:
        violations: list[str] = []
        _check_greptile(REPOSITORY_ROOT, violations)
        self.assertEqual([], violations)

    def test_missing_greptile_rule_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPOSITORY_ROOT / ".greptile", root / ".greptile")
            config_path = root / ".greptile/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["rules"] = [
                rule
                for rule in config["rules"]
                if rule["id"] != "alx-dynamic-reasoning"
            ]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            violations: list[str] = []
            _check_greptile(root, violations)
            self.assertTrue(
                any("alx-dynamic-reasoning" in violation for violation in violations),
                violations,
            )

    def test_automatic_greptile_reviews_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPOSITORY_ROOT / ".greptile", root / ".greptile")
            (root / "governance").mkdir()
            shutil.copy(
                REPOSITORY_ROOT / "governance/GREPTILE.sha256",
                root / "governance/GREPTILE.sha256",
            )
            config_path = root / ".greptile/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["triggerOnUpdates"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            violations: list[str] = []
            _check_greptile(root, violations)
            self.assertTrue(
                any(
                    "automatic commit re-reviews must remain disabled" in violation
                    for violation in violations
                ),
                violations,
            )


if __name__ == "__main__":
    unittest.main()
