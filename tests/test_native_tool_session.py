"""D-065: a Core reasoning session pauses on a native call and continues with its result.

The fake CLI below behaves as the Claude CLI does for this purpose: it starts
the MCP server named in --mcp-config (the real bridge), lists the tools the
request carries, calls two of them, reads each result, and finally prints a
JSON envelope whose structured output quotes both results. Nothing here runs a
real model.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.core.model_reasoner import native_tools  # noqa: E402
from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel  # noqa: E402

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    import json, subprocess, sys
    args = sys.argv[1:]
    config = json.loads(args[args.index("--mcp-config") + 1])
    allowed = args[args.index("--allowedTools") + 1].split(",")
    server = config["mcpServers"]["alx"]
    prompt = sys.stdin.read()
    bridge = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    def rpc(message_id, method, params):
        bridge.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": message_id, "method": method,
                                        "params": params}}) + "\\n")
        bridge.stdin.flush()
        return json.loads(bridge.stdout.readline())
    rpc(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    tools = rpc(2, "tools/list", {{}})["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert allowed == ["mcp__alx__" + name for name in names], (allowed, names)
    unknown = rpc(3, "tools/call", {{"name": "run_shell", "arguments": {{}}}})
    assert unknown["error"]["message"] == "unknown tool"
    seen = []
    for number, (name, arguments) in enumerate((
            ("select_goal", {{"goal_id": "g1"}}),
            ("call_capability", {{"call_id": "c2", "capability_id": "read_mail_message",
                                 "arguments_json": "{{}}", "approval_id": None,
                                 "approval_source_reference": None}}))):
        result = rpc(10 + number, "tools/call", {{"name": name, "arguments": arguments}})
        seen.append(result["result"]["content"][0]["text"])
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": {{"seen": seen, "prompt_chars": len(prompt),
                                            "tools": names}},
                      "usage": {{"input_tokens": 10, "output_tokens": 5}},
                      "modelUsage": {{"claude-sonnet-5-5": {{"inputTokens": 10}}}}}}))
''')


def request(**changes) -> ModelRequest:
    values = dict(
        messages=(ModelMessage(ModelRole.SYSTEM, "rules"), ModelMessage(ModelRole.USER, "context")),
        output_schema_name="decision",
        output_schema={"type": "object", "required": ["seen"]},
        tools=native_tools(),
    )
    values.update(changes)
    return ModelRequest(**values)


class NativeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cli = Path(directory.name) / "claude"
        cli.write_text(FAKE_CLI.format(python=sys.executable))
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("ANTHROPIC", "CLAUDE"))}
        self.model = ClaudeSubscriptionReasoningModel(
            "claude-sonnet-5-5", 30, executable=str(cli), environment=environment)
        self.addCleanup(self.model.abandon_session)

    def test_the_session_pauses_on_each_call_and_ends_with_the_decision(self) -> None:
        received = []
        first = self.model.complete(request(on_dispatched=lambda: received.append(1)))
        self.assertEqual(first.tool_use, {"name": "select_goal", "input": {"goal_id": "g1"}})
        self.assertEqual(first.output, {})
        # A native run is a dispatched request, as a one-shot run is.
        self.assertEqual(received, [1])
        second = self.model.complete(request(tool_result="goal one"))
        self.assertEqual(second.tool_use["name"], "call_capability")
        self.assertEqual(second.tool_use["input"]["call_id"], "c2")
        final = self.model.complete(request(tool_result="mail two"))
        self.assertIsNone(final.tool_use)
        self.assertEqual(list(final.output["seen"]), ["goal one", "mail two"])
        # Exactly the request's tools were listed and allowed.
        self.assertEqual(list(final.output["tools"]),
                         [item["name"] for item in native_tools()])
        # The prompt went in once, at the start; results continued the same run.
        self.assertGreater(final.output["prompt_chars"], 0)

    def test_a_result_for_a_lost_session_starts_a_fresh_one(self) -> None:
        self.model.complete(request())
        self.model.abandon_session()
        restarted = self.model.complete(request(tool_result="orphaned"))
        self.assertEqual(restarted.tool_use["name"], "select_goal")

    def test_a_new_request_abandons_a_paused_session(self) -> None:
        self.model.complete(request())
        fresh = self.model.complete(request())
        self.assertEqual(fresh.tool_use["name"], "select_goal")

    def test_an_abandoned_run_is_recorded_with_its_usage_unknown(self) -> None:
        """Review of #197: a run that reasoned is never missing from the record."""
        recorded = []
        self.model._telemetry_sink = lambda key, values: recorded.append(values)
        self.model.complete(request(affinity_key="c1"))
        self.model.abandon_session()
        (row,) = [item for item in recorded if item.get("outcome") == "abandoned"]
        self.assertEqual(row["code"], "reasoning.failed")
        self.assertTrue(row["usage_unknown"])

    def test_a_failed_launch_leaves_nothing_behind(self) -> None:
        import glob

        def refuse(*_args, **_kwargs):
            raise OSError("cannot start")

        before = set(glob.glob("/tmp/alx-native-*"))
        self.model._launcher = refuse
        with self.assertRaises(Exception):
            self.model.complete(request())
        self.assertEqual(set(glob.glob("/tmp/alx-native-*")), before)

    def test_a_cli_that_never_reads_a_large_prompt_cannot_hold_the_turn(self) -> None:
        import time
        from alx.providers.errors import ProviderError
        stalled = Path(tempfile.mkdtemp()) / "claude"
        self.addCleanup(lambda: stalled.unlink(missing_ok=True))
        stalled.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        stalled.chmod(stalled.stat().st_mode | stat.S_IEXEC)
        model = ClaudeSubscriptionReasoningModel("claude-sonnet-5-5", 2, executable=str(stalled))
        self.addCleanup(model.abandon_session)
        big = request(messages=(ModelMessage(ModelRole.SYSTEM, "rules"),
                                ModelMessage(ModelRole.USER, "x" * 2_000_000)))
        recorded = []
        model._telemetry_sink = lambda key, values: recorded.append(values)
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            model.complete(replace(big, affinity_key="c1"))
        self.assertLess(time.monotonic() - started, 30)
        # Each try is reported by its own failure; abandoning afterwards adds
        # no second record for a run already reported (review of #197).
        failures = len(recorded)
        model.abandon_session()
        self.assertEqual(len(recorded), failures)
        self.assertNotIn("abandoned", [item.get("outcome") for item in recorded])

    def test_a_tool_result_requires_a_native_request(self) -> None:
        with self.assertRaises(ValueError):
            request(tools=(), tool_result="x")

    def test_tool_names_are_unique(self) -> None:
        with self.assertRaises(ValueError):
            request(tools=(native_tools()[0], native_tools()[0]))


if __name__ == "__main__":
    unittest.main()
