"""D-065: a Core reasoning session pauses on a native call and continues with its result.

The fake CLI below behaves as the Claude CLI does for this purpose: it starts
the MCP server named in --mcp-config (the real bridge), calls the one tool,
reads the result, calls again, and finally prints a JSON envelope whose
structured output quotes both results. Nothing here runs a real model.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel  # noqa: E402

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    import json, subprocess, sys
    args = sys.argv[1:]
    config = json.loads(args[args.index("--mcp-config") + 1])
    assert args[args.index("--allowedTools") + 1] == "mcp__alx__call_capability"
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
    assert [tool["name"] for tool in tools] == ["call_capability"]
    seen = []
    for number in (1, 2):
        result = rpc(10 + number, "tools/call", {{"name": "call_capability", "arguments": {{
            "call_id": f"c{{number}}", "capability_id": "read_mail_message",
            "arguments_json": "{{}}", "approval_id": None}}}})
        seen.append(result["result"]["content"][0]["text"])
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": {{"seen": seen, "prompt_chars": len(prompt)}},
                      "usage": {{"input_tokens": 10, "output_tokens": 5}},
                      "modelUsage": {{"claude-sonnet-5-5": {{"inputTokens": 10}}}}}}))
''')


def request(**changes) -> ModelRequest:
    values = dict(
        messages=(ModelMessage(ModelRole.SYSTEM, "rules"), ModelMessage(ModelRole.USER, "context")),
        output_schema_name="decision",
        output_schema={"type": "object", "required": ["seen"]},
        native_tools=True,
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
            "claude-sonnet-5-5", 30, executable=str(cli), environment=environment,
            native_tools=True)
        self.addCleanup(self.model.abandon_session)

    def test_the_session_pauses_on_each_call_and_ends_with_the_decision(self) -> None:
        first = self.model.complete(request())
        self.assertEqual(first.tool_use["call_id"], "c1")
        self.assertEqual(first.output, {})
        second = self.model.complete(request(tool_result="mail one"))
        self.assertEqual(second.tool_use["call_id"], "c2")
        final = self.model.complete(request(tool_result="mail two"))
        self.assertIsNone(final.tool_use)
        self.assertEqual(list(final.output["seen"]), ["mail one", "mail two"])
        # The prompt went in once, at the start; results continued the same run.
        self.assertGreater(final.output["prompt_chars"], 0)

    def test_a_result_for_a_lost_session_starts_a_fresh_one(self) -> None:
        self.model.complete(request())
        self.model.abandon_session()
        restarted = self.model.complete(request(tool_result="orphaned"))
        self.assertEqual(restarted.tool_use["call_id"], "c1")

    def test_a_new_request_abandons_a_paused_session(self) -> None:
        self.model.complete(request())
        fresh = self.model.complete(request())
        self.assertEqual(fresh.tool_use["call_id"], "c1")

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
        model = ClaudeSubscriptionReasoningModel("claude-sonnet-5-5", 2, executable=str(stalled),
                                                 native_tools=True)
        self.addCleanup(model.abandon_session)
        big = request(messages=(ModelMessage(ModelRole.SYSTEM, "rules"),
                                ModelMessage(ModelRole.USER, "x" * 2_000_000)))
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            model.complete(big)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_tool_result_requires_a_native_request(self) -> None:
        with self.assertRaises(ValueError):
            request(native_tools=False, tool_result="x")


if __name__ == "__main__":
    unittest.main()
