"""Fixed canonical-main lifecycle authority and no-argument contracts."""

from __future__ import annotations

import subprocess
import sys
import unittest
import ast
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.repository import (  # noqa: E402
    REPOSITORY_INSPECT_PERMISSION,
    REPOSITORY_SYNCHRONIZE_PERMISSION,
    build_canonical_repository_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import CapabilityAttemptDisposition, CapabilityCall  # noqa: E402
from alx.providers.repository_runtime import CanonicalRepositoryRuntime  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.repository_runtime import (  # noqa: E402
    INSPECT_REPOSITORY_STATE,
    SYNCHRONIZE_LOCAL_MAIN,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
ROOT = REPOSITORY_ROOT


class Runner:
    def __init__(self, answers: dict[tuple[str, ...], list[tuple[int, str]]]) -> None:
        self.answers = {key: list(value) for key, value in answers.items()}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code, stdout = self.answers[argv].pop(0)
        return subprocess.CompletedProcess(argv, code, stdout, "secret stderr")


def answers(*, branch="main", status="", origin=SHA_B, fetch=0, merge=0):
    return {
        ("git", "rev-parse", "--show-toplevel"): [(0, str(ROOT)), (0, str(ROOT))],
        ("git", "config", "--get", "remote.origin.url"): [(0, "git@github.com:owner/repo.git"), (0, "git@github.com:owner/repo.git")],
        ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): [(0, branch), (0, branch)],
        ("git", "rev-parse", "--verify", "HEAD^{commit}"): [(0, SHA_A), (0, SHA_A), (0, SHA_B)],
        ("git", "rev-parse", "--verify", "refs/heads/main^{commit}"): [(0, SHA_A), (0, SHA_A)],
        ("git", "status", "--porcelain=v1", "--untracked-files=all"): [(0, status), (0, status)],
        ("git", "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"): [(fetch, "")],
        ("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): [(0, "")],
        ("git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"): [(0, origin), (0, origin)],
        ("git", "merge-base", "--is-ancestor", "refs/heads/main", "refs/remotes/origin/main"): [(0 if origin != SHA_A else 1, "")],
        ("git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", "refs/heads/main"): [(1, "")],
        ("git", "merge", "--ff-only", "refs/remotes/origin/main"): [(merge, "")],
    }


class RepositoryRuntimeTests(unittest.TestCase):
    def _provider(self, supplied=None):
        runner = Runner(supplied or answers())
        return CanonicalRepositoryRuntime(ROOT, "owner/repo", "git@github.com:owner/repo.git", 7, runner), runner

    def test_fast_forward_is_fixed_and_never_uses_a_shell(self):
        provider, runner = self._provider()
        result = provider.synchronize()
        self.assertEqual(result.transition, "fast_forwarded")
        self.assertEqual(result.local_after, SHA_B)
        self.assertIn(("git", "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"), [item[0] for item in runner.calls])
        self.assertIn(("git", "merge", "--ff-only", "refs/remotes/origin/main"), [item[0] for item in runner.calls])
        for _, kwargs in runner.calls:
            self.assertEqual(kwargs["cwd"], ROOT)
            self.assertFalse(kwargs["shell"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 7)

    def test_already_current_does_not_merge(self):
        provider, runner = self._provider(answers(origin=SHA_A))
        result = provider.synchronize()
        self.assertEqual(result.transition, "already_current")
        self.assertNotIn(("git", "merge", "--ff-only", "refs/remotes/origin/main"), [item[0] for item in runner.calls])

    def test_pre_merge_revalidation_refuses_a_stale_checkout(self):
        supplied = answers()
        supplied[("git", "symbolic-ref", "--quiet", "--short", "HEAD")] = [(0, "main"), (0, "topic")]
        provider, runner = self._provider(supplied)
        with self.assertRaisesRegex(Exception, "branch_not_main"):
            provider.synchronize()
        self.assertNotIn(("git", "merge", "--ff-only", "refs/remotes/origin/main"), [item[0] for item in runner.calls])

    def test_canonical_runtimes_share_one_checkout_lifecycle_lock(self):
        first, _ = self._provider(answers(origin=SHA_A))
        second, _ = self._provider(answers(origin=SHA_A))
        self.assertIs(first._lifecycle_lock, second._lifecycle_lock)

    def test_merge_base_operational_failure_is_not_ordinary_ancestry_false(self):
        supplied = answers()
        supplied[("git", "merge-base", "--is-ancestor", "refs/heads/main", "refs/remotes/origin/main")] = [(2, "")]
        provider, runner = self._provider(supplied)
        with self.assertRaisesRegex(Exception, "ancestry_failed"):
            provider.synchronize()
        self.assertNotIn(("git", "merge", "--ff-only", "refs/remotes/origin/main"), [item[0] for item in runner.calls])

    def test_untracked_and_wrong_branch_are_distinct_refusals(self):
        for supplied, code in ((answers(status="?? local.txt\n"), "worktree_untracked"), (answers(branch="topic"), "branch_not_main")):
            provider, _ = self._provider(supplied)
            with self.subTest(code=code), self.assertRaisesRegex(Exception, code):
                provider.inspect()

    def test_fetch_failure_is_declared_without_stderr(self):
        provider, _ = self._provider(answers(fetch=1))
        runtime = build_canonical_repository_runtime(True, ROOT, "owner/repo", "git@github.com:owner/repo.git", 7, lambda: "call", provider)
        broker = CapabilityBroker(CapabilityRegistry(runtime.definitions), SafetyGate(runtime.policies), runtime.executors)
        attempt = broker.dispatch(CapabilityCall("call", SYNCHRONIZE_LOCAL_MAIN, {}), AuthorityContext("alx", frozenset({REPOSITORY_SYNCHRONIZE_PERMISSION}), datetime.now(UTC)))
        self.assertEqual(attempt.result.failure["code"], "fetch_failed")
        self.assertNotIn("stderr", attempt.result.failure)

    def test_unexpected_adapter_error_has_its_own_declared_failure(self):
        runtime = build_canonical_repository_runtime(True, ROOT, "owner/repo", "git@github.com:owner/repo.git", 7, lambda: "call", type("Broken", (), {"inspect": lambda self: (_ for _ in ()).throw(AttributeError("internal")), "synchronize": lambda self: None})())
        broker = CapabilityBroker(CapabilityRegistry(runtime.definitions), SafetyGate(runtime.policies), runtime.executors)
        attempt = broker.dispatch(CapabilityCall("call", INSPECT_REPOSITORY_STATE, {}), AuthorityContext("alx", frozenset({REPOSITORY_INSPECT_PERMISSION}), datetime.now(UTC)))
        self.assertEqual(attempt.result.failure["code"], "repository_runtime_unavailable")

    def test_fixed_repository_process_site_has_no_generic_command_surface(self):
        source = (REPOSITORY_ROOT / "src/alx/providers/repository_runtime.py").read_text()
        tree = ast.parse(source)
        subprocess_attributes = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
        }
        self.assertEqual(subprocess_attributes, {"run", "CompletedProcess", "TimeoutExpired"})
        for forbidden in ("shell=True", "Popen", "git pull", "git reset", "git rebase", "git push", "git stash", "git remote"):
            self.assertNotIn(forbidden, source)

    def test_permissions_are_separate_and_inputs_are_empty(self):
        provider, _ = self._provider(answers(origin=SHA_A))
        runtime = build_canonical_repository_runtime(True, ROOT, "owner/repo", "git@github.com:owner/repo.git", 7, lambda: "call", provider)
        broker = CapabilityBroker(CapabilityRegistry(runtime.definitions), SafetyGate(runtime.policies), runtime.executors)
        denied = broker.dispatch(CapabilityCall("call", INSPECT_REPOSITORY_STATE, {}), AuthorityContext("alx", frozenset({REPOSITORY_SYNCHRONIZE_PERMISSION}), datetime.now(UTC)))
        self.assertIs(denied.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertFalse(denied.implementation_invoked)
        for definition in runtime.definitions:
            self.assertEqual(definition.input_schema.properties, {})
        allowed = broker.dispatch(CapabilityCall("call", INSPECT_REPOSITORY_STATE, {}), AuthorityContext("alx", frozenset({REPOSITORY_INSPECT_PERMISSION}), datetime.now(UTC)))
        self.assertTrue(allowed.implementation_invoked)


if __name__ == "__main__":
    unittest.main()
