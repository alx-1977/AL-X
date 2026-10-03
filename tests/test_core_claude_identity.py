"""AL/X Core reasons as her own Claude account, and never as anyone else's.

The dedicated Claude Code configuration directory is given to every Core
reasoning process; inherited identity overrides are dropped; the login is
verified through the CLI's machine-readable status before any turn; and a
missing or wrong login refuses visibly instead of falling back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from alx.bootstrap.providers import (  # noqa: E402
    build_runtime_providers, verify_core_claude_identity,
)
from alx.config import ConfigurationError, RuntimeSettings  # noqa: E402
from alx.config.settings import core_claude_identity  # noqa: E402
from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.providers.claude_subscription import (  # noqa: E402
    ClaudeSubscriptionReasoningModel, SubscriptionIdentityError,
)
from alx.providers.errors import ProviderError  # noqa: E402
from test_subscription_autonomy import BASE_ENVIRONMENT  # noqa: E402

ACCOUNT = "alx@fire-fli.co.za"


def status(config_dir, **changes):
    values = {
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
        "email": ACCOUNT, "subscriptionType": "team", "configDirectory": config_dir,
        "orgId": "org", "orgName": "Fire-Fli",
    }
    values.update(changes)
    return json.dumps(values)


def turn_envelope():
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": "", "structured_output": {"action": {}}})


def request():
    return ModelRequest(
        (ModelMessage(ModelRole.SYSTEM, "laws"), ModelMessage(ModelRole.USER, "hello")),
        "alx_core_decision", {"type": "object"}, "conversation-1",
    )


class Runner:
    """A fake process boundary: `auth status` answers status, a turn answers a turn."""

    def __init__(self, status_stdout):
        self.status_stdout = status_stdout
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        stdout = (self.status_stdout if command[1:] == ["auth", "status", "--json"]
                  else turn_envelope())
        return subprocess.CompletedProcess(command, 0, stdout, "")


class IdentityHarness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.dedicated = self.home / ".claude-alx"
        self.dedicated.mkdir()
        self.personal = self.home / ".claude"
        # The host environment AL/X runs in, holding everything that could
        # stand in for her identity or bill instead of the subscription.
        self.host = {
            "PATH": "/usr/bin", "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.personal),
            "CLAUDE_CODE_OAUTH_TOKEN": "personal-token",
            "ANTHROPIC_API_KEY": "metered-key",
        }

    def model(self, runner, *, config_dir=None, account=ACCOUNT):
        return ClaudeSubscriptionReasoningModel(
            "claude-opus-5-5", 60, runner=runner, environment=self.host,
            config_dir=str(self.dedicated if config_dir is None else config_dir),
            expected_account=account,
        )


class DedicatedDirectoryTests(IdentityHarness):
    def test_every_core_process_is_given_the_dedicated_directory(self):
        runner = Runner(status(str(self.dedicated)))
        self.model(runner).complete(request())
        self.assertEqual([call["command"][1:3] for call in runner.calls][0], ["auth", "status"])
        self.assertEqual(len(runner.calls), 2)
        for call in runner.calls:
            self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], str(self.dedicated))

    def test_the_host_environment_cannot_override_the_identity_or_bill(self):
        environment = self.model(Runner("")).child_environment()
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.dedicated))
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, environment)

    def test_the_identity_is_verified_before_every_turn(self):
        runner = Runner(status(str(self.dedicated)))
        model = self.model(runner)
        model.complete(request())
        model.complete(request())
        kinds = [call["command"][1] for call in runner.calls]
        self.assertEqual(kinds, ["auth", "--print", "auth", "--print"])

    def test_a_login_changed_while_running_stops_the_next_turn(self):
        runner = Runner(status(str(self.dedicated)))
        model = self.model(runner)
        model.complete(request())
        runner.status_stdout = status(str(self.dedicated), email="someone@else.example")
        with self.assertRaises(ProviderError):
            model.complete(request())
        self.assertEqual([call["command"][1] for call in runner.calls],
                         ["auth", "--print", "auth"])
        # And it stays refused: no later turn falls back to anyone.
        runner.status_stdout = status(str(self.dedicated))
        with self.assertRaises(ProviderError):
            model.complete(request())


class FailsVisiblyTests(IdentityHarness):
    def assert_refused(self, runner, code, **options):
        model = self.model(runner, **options)
        with self.assertRaises(SubscriptionIdentityError) as raised:
            model.verify_identity()
        self.assertEqual(raised.exception.code, code)
        # A turn is refused too, without any reasoning process.
        with self.assertRaises(ProviderError):
            model.complete(request())
        self.assertFalse(any(call["command"][1:2] == ["--print"] for call in runner.calls))
        # And it stays refused: there is no fallback identity to try next.
        with self.assertRaises(ProviderError):
            model.complete(request())

    def test_a_missing_directory_is_refused_before_the_cli_runs(self):
        runner = Runner(status(str(self.home / "absent")))
        self.assert_refused(runner, "subscription_config_missing",
                            config_dir=self.home / "absent")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.home / "absent").exists())

    def test_every_invalid_login_is_refused(self):
        dedicated = str(self.dedicated)
        cases = {
            "signed out": (status(dedicated, loggedIn=False), "subscription_unauthenticated"),
            "api key login": (status(dedicated, authMethod="apiKey"),
                              "subscription_login_not_claude_ai"),
            "third-party route": (status(dedicated, apiProvider="bedrock"),
                                  "subscription_login_not_claude_ai"),
            "another directory": (status(str(self.personal)), "subscription_config_mismatch"),
            "another account": (status(dedicated, email="friedl@fire-fli.co.za"),
                                "subscription_account_mismatch"),
            "unreadable status": ("Logged in as someone", "subscription_identity_unverifiable"),
        }
        for name, (stdout, code) in cases.items():
            with self.subTest(case=name):
                self.assert_refused(Runner(stdout), code)

    def test_startup_stops_with_a_configuration_error(self):
        settings = RuntimeSettings.from_environment(
            {**BASE_ENVIRONMENT, "HOME": str(self.home), "ALX_CLAUDE_ACCOUNT": ACCOUNT})
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            providers = build_runtime_providers(settings)
        with patch.object(ClaudeSubscriptionReasoningModel, "verify_identity",
                          side_effect=SubscriptionIdentityError("subscription_unauthenticated")):
            with self.assertRaisesRegex(ConfigurationError, "subscription_unauthenticated"):
                verify_core_claude_identity(providers)


class PersonalConfigurationTests(IdentityHarness):
    def test_the_personal_configuration_is_never_touched(self):
        self.personal.mkdir()
        (self.personal / ".claude.json").write_text('{"personal": true}')
        before = sorted(os.listdir(self.personal))
        runner = Runner(status(str(self.dedicated)))
        self.model(runner).complete(request())
        self.assertEqual(sorted(os.listdir(self.personal)), before)
        self.assertEqual((self.personal / ".claude.json").read_text(), '{"personal": true}')
        for call in runner.calls:
            self.assertNotIn(str(self.personal), call["env"].values())

    def test_configuration_cannot_point_the_core_at_the_personal_login(self):
        self.personal.mkdir()
        alias = self.home / "alias-to-personal"
        alias.symlink_to(self.personal)
        home_alias = self.home / "alias-to-home"
        home_alias.symlink_to(self.home)
        for value in ("~/.claude", "$HOME/.claude", str(self.personal), "~",
                      str(alias), str(home_alias), str(home_alias / ".claude")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigurationError, "not the personal"):
                    core_claude_identity({"HOME": str(self.home), "ALX_CLAUDE_ACCOUNT": ACCOUNT,
                                          "ALX_CLAUDE_CONFIG_DIR": value})

    def test_the_provider_refuses_a_directory_that_resolves_to_the_personal_login(self):
        self.personal.mkdir()
        alias = self.home / "alias-to-personal"
        alias.symlink_to(self.personal)
        runner = Runner(status(str(self.personal)))
        with self.assertRaises(SubscriptionIdentityError) as raised:
            self.model(runner, config_dir=alias).verify_identity()
        self.assertEqual(raised.exception.code, "subscription_config_personal")
        self.assertEqual(runner.calls, [])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = os.path.realpath(directory.name)
        self.environment = {"HOME": self.home, "ALX_CLAUDE_ACCOUNT": ACCOUNT}

    def test_the_default_is_beside_the_personal_configuration(self):
        self.assertEqual(core_claude_identity(self.environment),
                         (os.path.join(self.home, ".claude-alx"), ACCOUNT))

    def test_home_relative_explicit_and_aliased_directories_resolve(self):
        target = os.path.join(self.home, ".claude-core")
        os.mkdir(target)
        os.symlink(target, os.path.join(self.home, "core-alias"))
        for value in ("~/.claude-core", "$HOME/.claude-core", "${HOME}/x/../.claude-core",
                      target, os.path.join(self.home, "core-alias")):
            with self.subTest(value=value):
                self.assertEqual(
                    core_claude_identity({**self.environment, "ALX_CLAUDE_CONFIG_DIR": value}),
                    (target, ACCOUNT))

    def test_a_relative_directory_is_refused(self):
        with self.assertRaisesRegex(ConfigurationError, "absolute"):
            core_claude_identity({**self.environment, "ALX_CLAUDE_CONFIG_DIR": "claude"})

    def test_the_account_is_required(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ConfigurationError, "ALX_CLAUDE_ACCOUNT"):
                    core_claude_identity({"HOME": self.home, "ALX_CLAUDE_ACCOUNT": value})
        with self.assertRaisesRegex(ConfigurationError, "ALX_CLAUDE_ACCOUNT"):
            RuntimeSettings.from_environment({**BASE_ENVIRONMENT, "HOME": self.home,
                                              "ALX_CLAUDE_ACCOUNT": ""})
        with self.assertRaisesRegex(ValueError, "expected account"):
            ClaudeSubscriptionReasoningModel("claude-opus-5-5", 60,
                                             config_dir=os.path.join(self.home, ".claude-alx"))

    def test_both_subscription_cores_are_given_the_dedicated_identity(self):
        settings = RuntimeSettings.from_environment({
            **BASE_ENVIRONMENT, **self.environment,
            "ALX_AUTONOMOUS_PROVIDER": "claude_subscription",
            "ALX_AUTONOMOUS_MODEL": "claude-opus-5-5",
            # A host value naming another login has no say.
            "CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude"),
        })
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            providers = build_runtime_providers(settings)
        for model in (providers.reasoning, providers.autonomous):
            self.assertEqual(model.config_dir, os.path.join(self.home, ".claude-alx"))
            self.assertEqual(model._expected_account, ACCOUNT)

    def test_a_metered_core_has_no_claude_identity(self):
        settings = RuntimeSettings.from_environment({
            **BASE_ENVIRONMENT, "ALX_REASONING_PROVIDER": "openai",
            "ALX_REASONING_MODEL": "gpt", "OPENAI_API_KEY": "key",
        })
        self.assertIsNone(settings.core_claude_config_dir)


if __name__ == "__main__":
    unittest.main()
