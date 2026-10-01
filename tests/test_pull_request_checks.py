"""Reading GitHub check results for one pull request at one exact head.

The read reports what GitHub reported. It does not score a run, roll statuses
together, or rerun anything. A head that has moved is a different question, so
the read stops after naming that head. A log that cannot be fetched leaves the
check list in place and says so for that job alone.

No test here contacts GitHub. `httpx.get` on the provider module is replaced
the same way the merge and review providers are tested.
"""

from __future__ import annotations

import ast
import sys
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.pull_request_checks import (  # noqa: E402
    CHECKS_READ_PERMISSION,
    build_pull_request_checks_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    ContentOrigin,
    SideEffect,
    ValueKind,
)
from alx.contracts.pull_request_checks import (  # noqa: E402
    CHECK_READ_FAILURES,
    LOG_TAIL_CHARACTERS,
    CheckReadError,
    PullRequestChecksRequest,
)
from alx.core.model_reasoner import _result_fields  # noqa: E402
from alx.providers import github_checks  # noqa: E402
from alx.providers.github_checks import (  # noqa: E402
    API_ROOT,
    MAX_PAGES,
    PER_PAGE,
    USER_AGENT,
    GitHubPullRequestChecks,
)
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.pull_request_checks import (  # noqa: E402
    DEFINITION,
    READ_PULL_REQUEST_CHECKS,
    build_pull_request_checks_executors,
)


HEAD = "a" * 40
OTHER = "b" * 40
TOKEN = "ghp_test_token"
REPOSITORY = "owner/repo"
API_PREFIX = f"{API_ROOT}/repos/{REPOSITORY}"
BLOB = "https://blob.example/logs/"

# What a result is allowed to carry. A pass count, a rollup, or a blocking
# flag would be a judgement about the revision, and this read does not make one.
RUN_FIELDS = frozenset({
    "name",
    "status",
    "conclusion",
    "started_at",
    "completed_at",
    "details_url",
    "app",
    "output",
})
LOG_FIELDS = frozenset({
    "steps",
    "log_tail",
    "characters_omitted",
    "log_failure",
})
JUDGEMENT_FIELDS = frozenset({
    "passed",
    "failed",
    "failing",
    "blocking",
    "rollup",
    "overall",
    "summary",
    "verdict",
    "score",
})


def _run(
    name: str,
    *,
    status: str = "completed",
    conclusion: object = "success",
    app: object = None,
    details_url: object = None,
    html_url: object = None,
    identifier: int | None = None,
    url: object = None,
    title: object = None,
    summary: object = None,
    started_at: object = "2026-09-30T01:00:00Z",
    completed_at: object = "2026-09-30T01:01:00Z",
) -> dict:
    """One check run, with the extra GitHub keys a faithful copy must drop."""
    return {
        "id": identifier,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at,
        "details_url": details_url,
        "html_url": html_url,
        "url": url,
        "app": app,
        "output": {
            "title": title,
            "summary": summary,
            "annotations_count": 4,
        },
    }


def _actions(name: str, conclusion: object, job_id: int | None = None, **extra: object) -> dict:
    details = None if job_id is None else (
        f"https://github.com/{REPOSITORY}/actions/runs/15/job/{job_id}"
    )
    return _run(
        name,
        conclusion=conclusion,
        details_url=details,
        app={
            "slug": "github-actions",
            "name": "GitHub Actions",
            "id": 15368,
        },
        **extra,
    )


def _status(context: str, state: str, description: object, target_url: object) -> dict:
    return {
        "context": context,
        "state": state,
        "description": description,
        "target_url": target_url,
        "id": 1,
        "creator": {"login": "someone"},
    }


class _Response:
    def __init__(
        self,
        status: int,
        body: object = None,
        headers: dict | None = None,
        content: bytes | None = None,
        broken_json: bool = False,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.content = content
        self._broken_json = broken_json

    def json(self) -> object:
        if self._broken_json:
            raise ValueError("not json, and the body names ghp_test_token")
        return self._body


class GitHubScript:
    """The GET endpoints the production reader calls, and nothing else."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.pull_status = 200
        self.pull_headers: dict = {}
        self.pull_body: object = {"head": {"sha": HEAD}}
        self.pull_broken = False
        self.runs: list = []
        self.runs_body: object | None = None
        self.runs_status = 200
        self.runs_broken = False
        self.statuses: list = []
        self.statuses_body: object | None = None
        self.full_run_pages = 0
        self.jobs: dict[str, object] = {}
        self.job_status = 200
        self.logs: dict[str, bytes] = {}
        self.log_status = 200
        self.log_redirect = True
        self.log_location: str | None = None
        self.blob_error = False
        self.blob_status = 200
        self.workflow_runs: list = []
        self.workflow_jobs: dict[int, list] = {}
        self.transport = False

    def install(self, test: unittest.TestCase) -> None:
        module = github_checks.httpx
        originals = {
            name: getattr(module, name)
            for name in ("get", "post", "put", "patch", "delete", "request")
        }

        def get(url: str, **kwargs: object) -> _Response:
            if self.transport:
                raise module.ConnectError("dial tcp ghp_test_token")
            headers = dict(kwargs.get("headers") or {})
            self.calls.append({
                "method": "GET",
                "url": url,
                "headers": headers,
                "follow_redirects": kwargs.get("follow_redirects"),
                "timeout": kwargs.get("timeout"),
            })
            return self._answer(url, headers)

        def banned(method: str):
            def refuse(*_args: object, **_kwargs: object) -> None:
                self.calls.append({"method": method})
                raise AssertionError(f"check reader issued {method}")

            return refuse

        module.get = get
        for name in ("post", "put", "patch", "delete", "request"):
            setattr(module, name, banned(name.upper()))
        test.addCleanup(lambda: [setattr(module, name, originals[name]) for name in originals])

    def _answer(self, url: str, headers: dict) -> _Response:
        parts = urlsplit(url)
        if parts.netloc != urlsplit(API_ROOT).netloc:
            return self._blob(url, headers)
        path = parts.path
        page = int(parse_qs(parts.query).get("page", ["1"])[0])
        prefix = f"/repos/{REPOSITORY}"
        if not path.startswith(prefix):
            raise AssertionError(url)
        relative = path[len(prefix):]
        if relative == "/pulls/87":
            return _Response(
                self.pull_status,
                self.pull_body,
                self.pull_headers,
                broken_json=self.pull_broken,
            )
        if relative == f"/commits/{HEAD}/check-runs":
            if self.runs_broken:
                return _Response(200, broken_json=True)
            if self.runs_status != 200:
                return _Response(self.runs_status, {"message": "nope ghp_test_token"})
            if self.full_run_pages:
                if page > self.full_run_pages:
                    raise AssertionError(f"page {page} is past the ceiling")
                batch = [{"name": "x", "conclusion": "success"}] * PER_PAGE
                return _Response(200, {"check_runs": batch})
            if self.runs_body is not None and page == 1:
                return _Response(200, self.runs_body)
            batch = self.runs if page == 1 else []
            return _Response(200, {"check_runs": batch})
        if relative == f"/commits/{HEAD}/statuses":
            if self.statuses_body is not None and page == 1:
                return _Response(200, self.statuses_body)
            if page == 1:
                return _Response(200, self.statuses[:PER_PAGE])
            if page == 2:
                return _Response(200, self.statuses[PER_PAGE:])
            return _Response(200, [])
        if relative == f"/actions/runs" or relative.startswith("/actions/runs?"):
            return _Response(200, {"workflow_runs": self.workflow_runs})
        if relative.startswith("/actions/runs/") and relative.endswith("/jobs"):
            run_id = int(relative.split("/")[3])
            jobs = self.workflow_jobs.get(run_id, [])
            return _Response(200, {"jobs": jobs if page == 1 else []})
        if relative.startswith("/actions/jobs/") and relative.endswith("/logs"):
            job_id = relative.split("/")[3]
            if self.log_redirect:
                location = self.log_location
                if location is None:
                    location = f"{BLOB}{job_id}"
                return _Response(302, headers={"location": location})
            if self.log_status != 200:
                return _Response(
                    self.log_status, {"message": "log missing ghp_test_token"}
                )
            return _Response(200, content=self.logs.get(job_id, b""))
        if relative.startswith("/actions/jobs/"):
            job_id = relative.split("/")[3]
            if self.job_status != 200:
                return _Response(
                    self.job_status, {"message": "job missing ghp_test_token"}
                )
            body = self.jobs.get(job_id, {"steps": []})
            return _Response(200, body)
        raise AssertionError(url)

    def _blob(self, url: str, headers: dict) -> _Response:
        if self.blob_error:
            raise github_checks.httpx.ConnectError("blob dial ghp_test_token")
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        if self.blob_status != 200:
            return _Response(self.blob_status, content=b"nope")
        return _Response(200, content=self.logs.get(job_id, b""))


class ProviderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.github = GitHubScript()
        self.github.install(self)
        self.provider = GitHubPullRequestChecks(REPOSITORY, TOKEN)

    def read(self, number: int = 87, head: str = HEAD):
        return self.provider.read(PullRequestChecksRequest(number, head))

    def assert_get_only(self) -> None:
        self.assertGreaterEqual(len(self.github.calls), 1)
        self.assertTrue(all(call["method"] == "GET" for call in self.github.calls))
        for call in self.github.calls:
            self.assertIs(call["follow_redirects"], False)
            self.assertNotIn(TOKEN, call["url"])
            self.assertEqual(call["timeout"], github_checks.TIMEOUT_SECONDS)

    def assert_api_auth(self, call: dict) -> None:
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(call["headers"]["X-GitHub-Api-Version"], "2022-11-28")
        self.assertEqual(call["headers"]["User-Agent"], USER_AGENT)

    def urls(self) -> list[str]:
        return [call["url"] for call in self.github.calls]


class CatalogueContractTests(unittest.TestCase):
    """The registered description is a read, and it does not judge the result."""

    def test_the_capability_is_a_read_of_one_revision(self) -> None:
        self.assertEqual(DEFINITION.capability_id, READ_PULL_REQUEST_CHECKS)
        self.assertEqual(DEFINITION.capability_id, "read_pull_request_checks")
        self.assertIs(DEFINITION.side_effect, SideEffect.NONE)
        self.assertFalse(DEFINITION.transmits_authored_text)
        self.assertEqual(
            set(DEFINITION.input_schema.properties),
            {"pull_request_number", "head_sha"},
        )
        self.assertEqual(
            DEFINITION.input_schema.required,
            ("pull_request_number", "head_sha"),
        )
        self.assertIs(DEFINITION.input_schema.extra_properties, False)
        self.assertIs(
            DEFINITION.input_schema.properties["pull_request_number"].kind,
            ValueKind.INTEGER,
        )
        self.assertIs(
            DEFINITION.input_schema.properties["head_sha"].kind,
            ValueKind.STRING,
        )
        self.assertEqual(
            DEFINITION.durable_input_fields,
            ("pull_request_number", "head_sha"),
        )
        purpose = DEFINITION.purpose
        for phrase in ("rerun", "cancel", "merge", "score"):
            self.assertIn(phrase, purpose)

    def test_failure_codes_are_the_declared_set(self) -> None:
        self.assertEqual(
            DEFINITION.possible_failure_codes,
            (
                "arguments_unusable",
                "not_found",
                "head_changed",
                "permission_denied",
                "rate_limited",
                "provider_failed",
                "log_unavailable",
            ),
        )
        self.assertEqual(DEFINITION.possible_failure_codes, CHECK_READ_FAILURES)

    def test_result_fields_name_github_records_and_no_judgement(self) -> None:
        fields = _result_fields(DEFINITION.output_schema)
        self.assertEqual(
            fields,
            ["check_runs", "commit_statuses", "head_sha", "pull_request_number"],
        )
        self.assertTrue(JUDGEMENT_FIELDS.isdisjoint(fields))
        self.assertIs(DEFINITION.output_schema.extra_properties, False)
        run = DEFINITION.output_schema.properties["check_runs"].items
        self.assertIsNotNone(run)
        assert run is not None
        self.assertIs(run.extra_properties, False)
        self.assertTrue(JUDGEMENT_FIELDS.isdisjoint(run.properties))
        for name in (
            "name",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
            "details_url",
        ):
            self.assertIs(run.properties[name].kind, ValueKind.ANY)
        self.assertIs(run.properties["app"].extra_properties, False)
        self.assertEqual(set(run.properties["app"].properties), {"slug", "name"})
        self.assertIs(run.properties["output"].extra_properties, False)
        self.assertEqual(
            set(run.properties["output"].properties), {"title", "summary"}
        )
        status = DEFINITION.output_schema.properties["commit_statuses"].items
        assert status is not None
        self.assertEqual(
            set(status.properties),
            {"context", "state", "description", "target_url"},
        )
        self.assertIs(status.extra_properties, False)


class RegistrationTests(unittest.TestCase):
    """Configured with the repository and token merge already uses, or not at all."""

    def test_blank_configuration_registers_nothing(self) -> None:
        for repository, token in (
            ("", TOKEN),
            ("  ", TOKEN),
            (REPOSITORY, ""),
            (REPOSITORY, "  "),
        ):
            with self.subTest(repository=repository, token=token):
                self.assertIsNone(
                    build_pull_request_checks_runtime(
                        repository, token, lambda: "call-1"
                    )
                )

    def test_a_malformed_repository_registers_nothing(self) -> None:
        for repository in ("owner", "owner/repo/extra", "owner/../repo", "/repo"):
            with self.subTest(repository=repository):
                self.assertIsNone(
                    build_pull_request_checks_runtime(
                        repository, TOKEN, lambda: "call-1"
                    )
                )

    def test_the_policy_is_permission_without_approval_or_merge(self) -> None:
        runtime = build_pull_request_checks_runtime(
            REPOSITORY,
            TOKEN,
            lambda: "call-1",
            provider=types.SimpleNamespace(read=lambda *_args, **_kwargs: None),
        )
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(
            {item.capability_id for item in runtime.definitions},
            {READ_PULL_REQUEST_CHECKS},
        )
        self.assertEqual(set(runtime.executors), {READ_PULL_REQUEST_CHECKS})
        self.assertEqual(set(runtime.policies), {READ_PULL_REQUEST_CHECKS})
        self.assertEqual(runtime.permissions, frozenset({CHECKS_READ_PERMISSION}))
        self.assertEqual(CHECKS_READ_PERMISSION, "checks.read")
        policy = runtime.policies[READ_PULL_REQUEST_CHECKS]
        self.assertFalse(policy.approval_required)
        self.assertFalse(policy.standing_scope_allowed)
        self.assertEqual(
            policy.permission_references, frozenset({CHECKS_READ_PERMISSION})
        )
        for permission in runtime.permissions:
            self.assertNotIn("merge", permission)
            self.assertNotIn("review", permission)
        self.assertNotIn("repository.merge", runtime.permissions)
        self.assertNotIn("review.request", runtime.permissions)

    def test_permission_alone_authorises_the_read(self) -> None:
        runtime = build_pull_request_checks_runtime(
            REPOSITORY,
            TOKEN,
            lambda: "call-1",
            provider=types.SimpleNamespace(read=lambda *_args, **_kwargs: None),
        )
        assert runtime is not None
        gate = SafetyGate(runtime.policies)
        call = CapabilityCall(
            "call-1",
            READ_PULL_REQUEST_CHECKS,
            {"pull_request_number": 87, "head_sha": HEAD},
        )
        allowed = gate.evaluate(
            call,
            AuthorityContext(
                "alx", frozenset({CHECKS_READ_PERMISSION}), datetime.now(UTC)
            ),
        )
        self.assertTrue(allowed.allowed)
        missing = gate.evaluate(
            call,
            AuthorityContext("alx", frozenset(), datetime.now(UTC)),
        )
        self.assertEqual(missing.reason, "permission_missing")

    def test_live_composition_uses_the_merge_repository_and_token(self) -> None:
        """The read is not behind the merge or review enable switches."""
        source = (
            REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_pull_request_checks_runtime"
        ]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(len(call.args), 3)
        self.assertEqual(call.keywords, [])
        repository_arg, token_arg = call.args[0], call.args[1]
        self.assertIsInstance(repository_arg, ast.Attribute)
        self.assertEqual(repository_arg.attr, "repository")
        self.assertIsInstance(repository_arg.value, ast.Name)
        self.assertEqual(repository_arg.value.id, "merge_configuration")
        self.assertIsInstance(token_arg, ast.Attribute)
        self.assertEqual(token_arg.attr, "token")
        self.assertIsInstance(token_arg.value, ast.Name)
        self.assertEqual(token_arg.value.id, "merge_configuration")
        self.assertIsInstance(call.args[2], ast.Lambda)
        parent = parents.get(id(call))
        while parent is not None:
            self.assertNotIsInstance(parent, ast.If)
            parent = parents.get(id(parent))
        start = source.index("checks_runtime = build_pull_request_checks_runtime(")
        window = source[start:start + 700]
        self.assertIn("registry.register(definition)", window)
        self.assertIn("policies.update(checks_runtime.policies)", window)
        self.assertIn("executors.update(checks_runtime.executors)", window)
        self.assertIn("permissions.update(checks_runtime.permissions)", window)


class ExecutorTests(unittest.TestCase):
    """Invalid input and provider failures stay structured, and carry no prose."""

    def executor(self, reader):
        return build_pull_request_checks_executors(reader, lambda: "call-1")[
            READ_PULL_REQUEST_CHECKS
        ]

    def test_unusable_arguments_do_not_read(self) -> None:
        seen: list = []

        def reader(request):
            seen.append(request)
            raise AssertionError("unusable arguments were sent to GitHub")

        execute = self.executor(reader)
        samples = (
            {"head_sha": HEAD},
            {"pull_request_number": 87},
            {"pull_request_number": True, "head_sha": HEAD},
            {"pull_request_number": 0, "head_sha": HEAD},
            {"pull_request_number": -3, "head_sha": HEAD},
            {"pull_request_number": 87, "head_sha": "A" * 40},
            {"pull_request_number": 87, "head_sha": "a" * 39},
            {"pull_request_number": 87, "head_sha": HEAD + "\n"},
        )
        for arguments in samples:
            with self.subTest(arguments=arguments):
                result = execute(arguments)
                self.assertIs(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], "arguments_unusable")
                self.assertFalse(result.failure["requires_judgement"])
                self.assertEqual(set(result.failure), {"code", "requires_judgement"})
        self.assertEqual(seen, [])

    def test_head_changed_carries_the_actual_head_only(self) -> None:
        def reader(_request):
            raise CheckReadError("head_changed", actual_head=OTHER)

        result = self.executor(reader)(
            {"pull_request_number": 87, "head_sha": HEAD}
        )
        self.assertEqual(result.failure["code"], "head_changed")
        self.assertEqual(result.failure["actual_head"], OTHER)
        self.assertEqual(
            set(result.failure), {"code", "actual_head", "requires_judgement"}
        )
        self.assertTrue(result.failure["requires_judgement"])
        self.assertNotIn(TOKEN, str(dict(result.failure)))

    def test_a_provider_exception_returns_its_type_and_not_its_text(self) -> None:
        def reader(_request):
            raise RuntimeError(f"request failed with {TOKEN}")

        execute = self.executor(reader)
        with self.assertLogs("alx.tools.pull_request_checks", level="WARNING") as logs:
            result = execute({"pull_request_number": 87, "head_sha": HEAD})
        self.assertEqual(result.failure["code"], "provider_failed")
        self.assertEqual(set(result.failure), {"code", "requires_judgement"})
        rendered = "\n".join(logs.output)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn(TOKEN, str(dict(result.failure)))

    def test_a_declared_failure_does_not_gain_the_exception_text(self) -> None:
        def reader(_request):
            raise CheckReadError("not_found")

        result = self.executor(reader)(
            {"pull_request_number": 87, "head_sha": HEAD}
        )
        self.assertEqual(result.failure["code"], "not_found")
        self.assertNotIn("actual_head", result.failure)


class GitHubReadTests(ProviderCase):
    """What GitHub returned, copied, and the reads that stop or narrow."""

    def _broker(self):
        runtime = build_pull_request_checks_runtime(
            REPOSITORY, TOKEN, lambda: "call-1", provider=self.provider
        )
        assert runtime is not None
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        return broker

    def _dispatch(self, arguments: dict):
        return self._broker().dispatch(
            CapabilityCall("call-1", READ_PULL_REQUEST_CHECKS, arguments),
            AuthorityContext(
                "alx", frozenset({CHECKS_READ_PERMISSION}), datetime.now(UTC)
            ),
        )

    def test_passing_and_failing_runs_are_copied_with_a_tailed_log(self) -> None:
        """Passing, failing, and still-running runs come back as GitHub sent them.

        The failing Actions job is the only one whose steps and log are read.
        The log keeps a suffix and counts the characters left off it. Statuses
        continue onto a second page and are not collapsed.
        """
        prefix = "P" * 17
        kept = ("K" * (LOG_TAIL_CHARACTERS - 1)) + "é"
        self.github.runs = [
            _actions(
                "lint",
                "success",
                job_id=9,
                title="lint",
                summary="  clean\n",
                started_at="2026-09-30T01:00:00Z",
                completed_at="2026-09-30T01:01:00Z",
            ),
            _actions("lint", "success", job_id=9, title="lint", summary="again"),
            _actions(
                "still-running",
                None,
                status="in_progress",
                started_at="2026-09-30T01:02:03Z",
                completed_at=None,
                title=None,
                summary=None,
            ),
            _actions(
                "law-gates",
                "failure",
                job_id=42,
                title="law gates failed",
                summary="  line one\nline two  ",
                started_at="2026-09-30T01:03:00Z",
                completed_at="2026-09-30T01:04:00Z",
            ),
            _run(
                "external-ci",
                conclusion="failure",
                app={"slug": "circleci", "name": "CircleCI", "id": 9},
                details_url="https://circleci.com/build/1",
                title="circle",
                summary=None,
            ),
        ]
        # The in-progress run has no job URL. The duplicate lint shares job 9,
        # which a success must not fetch. Replace the third run's details.
        self.github.runs[2]["details_url"] = None
        self.github.jobs["42"] = {
            "id": 42,
            "steps": [
                {
                    "name": "Checkout",
                    "conclusion": "success",
                    "number": 1,
                    "status": "completed",
                },
                {
                    "name": "Law gates",
                    "conclusion": "failure",
                    "started_at": "2026-09-30T01:03:30Z",
                },
            ],
        }
        self.github.logs["42"] = (prefix + kept).encode("utf-8")
        page_one = [
            _status(f"ctx-{index}", "success", f"desc-{index}", f"https://example.test/{index}")
            for index in range(PER_PAGE)
        ]
        page_two = [
            _status("ci", "pending", "first", "https://example.test/first"),
            _status("ci", "success", "second", None),
        ]
        self.github.statuses = page_one + page_two

        attempt = self._dispatch({"pull_request_number": 87, "head_sha": HEAD})
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        result = attempt.result
        self.assertEqual(
            result.durable_values,
            {"pull_request_number": 87, "head_sha": HEAD},
        )
        self.assertNotIn("check_runs", result.durable_values)
        self.assertNotIn("log_tail", result.durable_values)
        self.assertEqual(
            result.provenance.origins, frozenset({ContentOrigin.EXTERNAL})
        )
        self.assertIsNone(result.provenance.content_expires_at)

        values = result.values
        self.assertEqual(values["pull_request_number"], 87)
        self.assertEqual(values["head_sha"], HEAD)
        runs = values["check_runs"]
        self.assertEqual(
            [item["name"] for item in runs],
            ["lint", "lint", "still-running", "law-gates", "external-ci"],
        )
        lint, again, running, failed, external = runs
        self.assertEqual(set(lint), RUN_FIELDS)
        self.assertEqual(set(again), RUN_FIELDS)
        self.assertEqual(lint["conclusion"], "success")
        self.assertEqual(lint["output"], {"title": "lint", "summary": "  clean\n"})
        self.assertEqual(
            lint["app"], {"slug": "github-actions", "name": "GitHub Actions"}
        )
        self.assertEqual(running["status"], "in_progress")
        self.assertIsNone(running["conclusion"])
        self.assertIsNone(running["completed_at"])
        self.assertIsNone(running["details_url"])
        self.assertEqual(running["output"], {"title": None, "summary": None})
        self.assertEqual(set(running), RUN_FIELDS)
        self.assertEqual(failed["conclusion"], "failure")
        self.assertEqual(failed["started_at"], "2026-09-30T01:03:00Z")
        self.assertEqual(failed["completed_at"], "2026-09-30T01:04:00Z")
        self.assertEqual(
            failed["output"],
            {"title": "law gates failed", "summary": "  line one\nline two  "},
        )
        self.assertEqual(
            tuple(failed["steps"]),
            (
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Law gates", "conclusion": "failure"},
            ),
        )
        self.assertEqual(failed["log_tail"], kept)
        self.assertEqual(failed["characters_omitted"], 17)
        self.assertNotIn("log_failure", failed)
        self.assertEqual(set(failed), RUN_FIELDS | frozenset({
            "steps", "log_tail", "characters_omitted",
        }))
        self.assertEqual(external["conclusion"], "failure")
        self.assertEqual(external["app"], {"slug": "circleci", "name": "CircleCI"})
        self.assertIsNone(external["output"]["summary"])
        self.assertEqual(set(external), RUN_FIELDS)

        statuses = values["commit_statuses"]
        self.assertEqual(len(statuses), PER_PAGE + 2)
        self.assertEqual(statuses[0]["context"], "ctx-0")
        self.assertEqual(statuses[-2]["description"], "first")
        self.assertEqual(statuses[-1]["context"], "ci")
        self.assertEqual(statuses[-1]["description"], "second")
        self.assertIsNone(statuses[-1]["target_url"])
        self.assertEqual(
            set(statuses[0]), {"context", "state", "description", "target_url"}
        )

        self.assert_get_only()
        job_urls = [url for url in self.urls() if "/actions/jobs/" in url]
        self.assertEqual(
            [urlsplit(url).path for url in job_urls],
            [
                f"/repos/{REPOSITORY}/actions/jobs/42",
                f"/repos/{REPOSITORY}/actions/jobs/42/logs",
            ],
        )
        blob_calls = [
            call for call in self.github.calls if call["url"].startswith(BLOB)
        ]
        self.assertEqual(len(blob_calls), 1)
        self.assertEqual(blob_calls[0]["url"], f"{BLOB}42")
        names = {key.lower() for key in blob_calls[0]["headers"]}
        self.assertNotIn("authorization", names)
        self.assertNotIn(TOKEN, " ".join(blob_calls[0]["headers"].values()))
        self.assertEqual(blob_calls[0]["headers"], {"User-Agent": USER_AGENT})
        for call in self.github.calls:
            if call["url"].startswith(API_ROOT):
                self.assert_api_auth(call)
        status_urls = [url for url in self.urls() if "/statuses?" in url]
        self.assertEqual(
            [urlsplit(url).query for url in status_urls],
            [f"per_page={PER_PAGE}&page=1", f"per_page={PER_PAGE}&page=2"],
        )
        check_pages = [
            urlsplit(url).query
            for url in self.urls()
            if "/check-runs?" in url
        ]
        self.assertEqual(check_pages, [f"per_page={PER_PAGE}&page=1"])

    def test_other_failing_conclusions_are_logged_and_skipped_is_not(self) -> None:
        self.github.runs = [
            _actions("timed", "timed_out", job_id=2),
            _actions("cancelled-job", "cancelled", job_id=3),
            _actions("needs-action", "action_required", job_id=4),
            _actions("skipped-job", "skipped", job_id=8),
            _actions("startup", "startup_failure", job_id=6),
        ]
        for job_id, text in (("2", "timed out\n"), ("3", "cancelled\n"), ("4", "act\n")):
            self.github.logs[job_id] = text.encode()
            self.github.jobs[job_id] = {
                "steps": [{"name": f"step-{job_id}", "conclusion": "failure"}]
            }
        content = self.read()
        by_name = {item.name: item for item in content.check_runs}
        for name, job_id, text in (
            ("timed", "2", "timed out\n"),
            ("cancelled-job", "3", "cancelled\n"),
            ("needs-action", "4", "act\n"),
        ):
            record = by_name[name]
            self.assertEqual(record.log_tail, text)
            self.assertEqual(record.characters_omitted, 0)
            self.assertIsNone(record.log_failure)
            self.assertEqual(record.steps, ((f"step-{job_id}", "failure"),))
        for name in ("skipped-job", "startup"):
            self.assertIsNone(by_name[name].log_tail)
            self.assertIsNone(by_name[name].steps)
        fetched = {urlsplit(url).path.rsplit("/", 1)[-1] for url in self.urls()}
        self.assertTrue({"2", "3", "4"} <= fetched)
        self.assertNotIn("8", fetched)
        self.assertNotIn("6", fetched)
        self.assert_get_only()

    def test_a_non_actions_failure_requests_no_job_and_no_log(self) -> None:
        self.github.runs = [
            _run(
                "external-ci",
                conclusion="failure",
                app={"slug": "circleci", "name": "CircleCI"},
                details_url="https://circleci.com/build/99",
                title="nope",
                summary="broke",
            )
        ]
        content = self.read()
        self.assertEqual(len(content.check_runs), 1)
        self.assertIsNone(content.check_runs[0].log_tail)
        self.assertIsNone(content.check_runs[0].steps)
        for url in self.urls():
            self.assertNotIn("/actions/", url)
            self.assertFalse(url.startswith(BLOB))
        self.assert_get_only()
        self.assertEqual(len(self.github.calls), 3)

    def test_a_head_that_moved_is_named_and_nothing_else_is_read(self) -> None:
        self.github.pull_body = {"head": {"sha": OTHER}, "number": 87}
        self.github.runs = [_actions("law-gates", "failure", job_id=42)]
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "head_changed")
        self.assertEqual(caught.exception.actual_head, OTHER)
        self.assertEqual(len(self.github.calls), 1)
        self.assertTrue(self.urls()[0].endswith("/pulls/87"))
        self.assert_get_only()

    def test_a_missing_pull_request_is_not_found(self) -> None:
        self.github.pull_status = 404
        self.github.pull_body = {"message": f"Not Found {TOKEN}"}
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "not_found")
        self.assertIsNone(caught.exception.actual_head)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(len(self.github.calls), 1)

    def test_refusal_throttling_and_provider_failures_are_distinct(self) -> None:
        cases = (
            (401, {}, "permission_denied"),
            (403, {}, "permission_denied"),
            (403, {"x-ratelimit-remaining": "4999"}, "permission_denied"),
            (403, {"x-ratelimit-remaining": "0"}, "permission_denied"),
            (403, {"retry-after": "60"}, "rate_limited"),
            (
                403,
                {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
                "rate_limited",
            ),
            (429, {}, "rate_limited"),
            (500, {}, "provider_failed"),
        )
        for status, headers, code in cases:
            with self.subTest(status=status, headers=headers):
                self.github.calls.clear()
                self.github.pull_status = status
                self.github.pull_headers = headers
                self.github.pull_body = {"message": f"nope {TOKEN}"}
                with self.assertRaises(CheckReadError) as caught:
                    self.read()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertEqual(len(self.github.calls), 1)
                self.assertEqual(self.github.calls[0]["method"], "GET")

    def test_transport_and_non_json_are_provider_failed(self) -> None:
        self.github.transport = True
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(TOKEN, str(caught.exception))

        self.github.transport = False
        self.github.pull_broken = True
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(len(self.github.calls), 1)

    def test_an_unexpected_body_is_not_an_empty_success(self) -> None:
        self.github.pull_body = ["not", "a", "pull"]
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")

        self.github.pull_body = {"head": {"sha": HEAD}}
        self.github.runs_body = {"total_count": 3}
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")
        self.assertNotIn("/statuses", " ".join(self.urls()))

        self.github.runs_body = None
        self.github.runs = []
        self.github.statuses_body = {
            "state": "failure",
            "statuses": [_status("ci", "failure", "rolled up", None)],
            "total_count": 1,
        }
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")

    def test_an_empty_list_is_a_real_empty_result(self) -> None:
        content = self.read()
        self.assertEqual(content.check_runs, ())
        self.assertEqual(content.commit_statuses, ())
        self.assertEqual(content.head_sha, HEAD)

    def test_a_full_page_at_the_ceiling_is_not_returned_short(self) -> None:
        self.github.full_run_pages = MAX_PAGES
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "provider_failed")
        pages = [
            parse_qs(urlsplit(url).query).get("page", ["1"])[0]
            for url in self.urls()
            if "/check-runs" in url
        ]
        self.assertEqual(pages, [str(page) for page in range(1, MAX_PAGES + 1)])
        self.assertTrue(all("/statuses" not in url for url in self.urls()))

    def test_a_missing_log_leaves_the_check_list(self) -> None:
        self.github.runs = [
            _actions("lint", "success", job_id=9, title="lint", summary="clean"),
            _actions("law-gates", "failure", job_id=42, title="no", summary="no"),
        ]
        self.github.jobs["42"] = {
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Law gates", "conclusion": "failure"},
            ]
        }
        self.github.log_redirect = False
        self.github.log_status = 404
        content = self.read()
        self.assertEqual(
            [item.name for item in content.check_runs], ["lint", "law-gates"]
        )
        failed = content.check_runs[1]
        self.assertEqual(
            failed.steps,
            (("Checkout", "success"), ("Law gates", "failure")),
        )
        self.assertEqual(failed.log_tail, "")
        self.assertEqual(failed.characters_omitted, 0)
        self.assertEqual(failed.log_failure, "log_unavailable")
        self.assertIsNone(content.check_runs[0].log_tail)
        self.assert_get_only()

    def test_a_redirect_that_cannot_be_read_is_the_same_per_job_failure(self) -> None:
        self.github.runs = [
            _actions("law-gates", "failure", job_id=42, title="no", summary="no")
        ]
        self.github.jobs["42"] = {"steps": [{"name": "Run", "conclusion": "failure"}]}
        self.github.log_location = ""
        content = self.read()
        failed = content.check_runs[0]
        self.assertEqual(failed.steps, (("Run", "failure"),))
        self.assertEqual(failed.log_failure, "log_unavailable")
        self.assertEqual(failed.log_tail, "")
        self.assertEqual(failed.characters_omitted, 0)
        self.assertTrue(all(call["url"].startswith(API_ROOT) for call in self.github.calls))

        self.github.calls.clear()
        self.github.log_location = None
        self.github.blob_error = True
        content = self.read()
        failed = content.check_runs[0]
        self.assertEqual(failed.log_failure, "log_unavailable")
        self.assertEqual(failed.steps, (("Run", "failure"),))
        blob_calls = [
            call for call in self.github.calls if call["url"].startswith(BLOB)
        ]
        self.assertEqual(len(blob_calls), 1)
        self.assertNotIn("authorization", {key.lower() for key in blob_calls[0]["headers"]})
        self.assertNotIn(TOKEN, " ".join(blob_calls[0]["headers"].values()))

    def test_a_job_that_cannot_be_read_does_not_invent_one(self) -> None:
        self.github.runs = [
            _actions("law-gates", "failure", job_id=42, title="no", summary="no")
        ]
        self.github.job_status = 404
        content = self.read()
        failed = content.check_runs[0]
        self.assertIsNone(failed.steps)
        self.assertEqual(failed.log_failure, "log_unavailable")
        self.assertEqual(failed.log_tail, "")
        self.assertEqual(failed.characters_omitted, 0)
        self.assertEqual(failed.conclusion, "failure")
        self.assertTrue(all(not url.endswith("/logs") for url in self.urls()))

    def test_a_job_id_in_the_html_url_is_used_before_listing_runs(self) -> None:
        run = _actions("law-gates", "failure", title="no", summary="no")
        run["details_url"] = f"https://github.com/{REPOSITORY}/actions/runs/15"
        run["html_url"] = f"https://github.com/{REPOSITORY}/actions/runs/15/job/42"
        self.github.runs = [run]
        self.github.jobs["42"] = {"steps": [{"name": "Run", "conclusion": "failure"}]}
        self.github.logs["42"] = b"from the job url"
        content = self.read()
        self.assertEqual(content.check_runs[0].log_tail, "from the job url")
        self.assertTrue(all("/actions/runs" not in url for url in self.urls()))
        self.assertTrue(any(url.endswith("/actions/jobs/42") for url in self.urls()))

    def test_a_job_id_is_taken_from_the_workflow_run_when_urls_lack_one(self) -> None:
        run = _actions("law-gates", "failure", title="no", summary="no", identifier=555)
        run["details_url"] = f"https://github.com/{REPOSITORY}/actions/runs/15"
        run["html_url"] = f"https://github.com/{REPOSITORY}/actions/runs/15"
        run["url"] = f"{API_ROOT}/repos/{REPOSITORY}/check-runs/555"
        self.github.runs = [run]
        self.github.workflow_runs = [{"id": 15}, {"id": 16}]
        self.github.workflow_jobs = {
            15: [{
                "id": 77,
                "check_run_url": f"{API_ROOT}/repos/{REPOSITORY}/check-runs/555",
            }],
            16: [{
                "id": 88,
                "check_run_url": f"{API_ROOT}/repos/{REPOSITORY}/check-runs/999",
            }],
        }
        self.github.jobs["77"] = {"steps": [{"name": "Matched", "conclusion": "failure"}]}
        self.github.logs["77"] = b"matched"
        content = self.read()
        self.assertEqual(content.check_runs[0].log_tail, "matched")
        self.assertEqual(content.check_runs[0].steps, (("Matched", "failure"),))
        self.assertTrue(any(f"head_sha={HEAD}" in url for url in self.urls()))
        self.assertTrue(any(url.endswith("/actions/jobs/77") for url in self.urls()))
        self.assertTrue(all(not url.endswith("/actions/jobs/88") for url in self.urls()))
        self.assert_get_only()
        blob_calls = [call for call in self.github.calls if "/logs" in call["url"]
                      and not call["url"].startswith(API_ROOT)]
        self.assertEqual(len(blob_calls), 1)
        self.assertNotIn("Authorization", blob_calls[0]["headers"])
        self.assertNotIn(TOKEN, blob_calls[0]["url"])

    def test_an_unmatched_workflow_job_is_unavailable_rather_than_guessed(self) -> None:
        run = _actions("law-gates", "failure", title="no", summary="no", identifier=555)
        run["details_url"] = f"https://github.com/{REPOSITORY}/actions/runs/15"
        run["html_url"] = None
        run["url"] = f"{API_ROOT}/repos/{REPOSITORY}/check-runs/555"
        self.github.runs = [run]
        self.github.workflow_runs = [{"id": 15}]
        self.github.workflow_jobs = {
            15: [{
                "id": 88,
                "check_run_url": f"{API_ROOT}/repos/{REPOSITORY}/check-runs/999",
            }]
        }
        content = self.read()
        failed = content.check_runs[0]
        self.assertEqual(failed.log_failure, "log_unavailable")
        self.assertEqual(failed.log_tail, "")
        self.assertEqual(failed.characters_omitted, 0)
        self.assertIsNone(failed.steps)
        self.assertTrue(all("/actions/jobs/" not in url for url in self.urls()))

    def test_later_throttling_does_not_return_a_partial_list(self) -> None:
        self.github.runs_status = 429
        with self.assertRaises(CheckReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.github.calls), 2)
        self.assertTrue(all(call["method"] == "GET" for call in self.github.calls))

    def test_broker_rejects_a_call_the_schema_does_not_describe(self) -> None:
        attempt = self._dispatch(
            {"pull_request_number": 87, "head_sha": HEAD, "rerun": True}
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "input_invalid")
        self.assertFalse(attempt.implementation_invoked)
        self.assertEqual(self.github.calls, [])

        attempt = self._dispatch({"pull_request_number": 0, "head_sha": HEAD})
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertEqual(self.github.calls, [])

        attempt = self._dispatch({"pull_request_number": 87, "head_sha": "A" * 40})
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertEqual(self.github.calls, [])


class ReadOnlyBoundaryTests(unittest.TestCase):
    """One GET client, the existing token argument, and no second auth path."""

    def test_the_provider_only_gets(self) -> None:
        source_path = REPOSITORY_ROOT / "src/alx/providers/github_checks.py"
        text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        attrs = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "httpx"
        ]
        self.assertEqual(set(attrs), {"get", "HTTPError"})
        for banned in (
            "httpx.Client",
            "httpx.post",
            "httpx.put",
            "httpx.patch",
            "httpx.delete",
            "httpx.request",
            "os.environ",
            "getenv",
            "GITHUB_TOKEN",
            "ALX_",
        ):
            self.assertNotIn(banned, text)

    def test_the_tool_and_bootstrap_do_not_grow_a_client_or_a_token_source(self) -> None:
        for relative in (
            "src/alx/tools/pull_request_checks.py",
            "src/alx/bootstrap/pull_request_checks.py",
            "src/alx/contracts/pull_request_checks.py",
        ):
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            for banned in (
                "httpx",
                "os.environ",
                "getenv",
                "GITHUB_TOKEN",
                "ALX_MERGE_ENABLED",
                "ALX_REVIEW_REQUEST_ENABLED",
            ):
                self.assertNotIn(banned, text, relative)

    def test_constructor_rejects_a_repository_that_would_build_a_wrong_url(self) -> None:
        for repository in ("/repo", "owner/", "owner/repo/extra", "ownerrepo", ""):
            with self.subTest(repository=repository):
                with self.assertRaises(ValueError):
                    GitHubPullRequestChecks(repository, TOKEN)
        with self.assertRaises(ValueError):
            GitHubPullRequestChecks(REPOSITORY, " ")
        built = GitHubPullRequestChecks(f"  {REPOSITORY}  ", f"  {TOKEN}  ")
        self.assertIn(REPOSITORY, built._url("/pulls/87"))


if __name__ == "__main__":
    unittest.main()
