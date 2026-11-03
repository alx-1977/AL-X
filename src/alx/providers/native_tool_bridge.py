"""Relay the Core's native tool calls from the Claude CLI to the waiting runtime.

The Claude CLI starts this as a stdio MCP server for one reasoning session. It
offers exactly one tool, `call_capability`, and decides nothing about it: each
call is written to the runtime's socket, and whatever the runtime answers is
returned to the model as the tool result. Which capability may run, whether
it is safe, and what its result means all stay with the Core, which handles a
native call exactly as it handles the same call in a decision (D-065).

Calls are answered one at a time, in the order they arrive, because the Core
handles one step at a time.

Standard library only: it runs in the CLI's process tree, not the runtime's.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

TOOL_NAME = "call_capability"
TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Call one capability from your capability catalogue and see its result "
        "before you decide what to do next. Call as many as the work needs, one "
        "after another. The runtime applies exactly the checks a decision's "
        "call_capability action gets; a refused call returns the reason."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "description": "A new id, unique in this turn."},
            "capability_id": {"type": "string",
                              "description": "One id from the capability catalogue."},
            "arguments_json": {"type": "string",
                               "description": "The call's arguments as a JSON object."},
            "approval_id": {"type": ["string", "null"],
                            "description": "A granted approval this call uses, or null."},
        },
        "required": ["call_id", "capability_id", "arguments_json", "approval_id"],
        "additionalProperties": False,
    },
}


def _reply(message_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(message_id: Any, code: int, text: str) -> None:
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text},
    }) + "\n")
    sys.stdout.flush()


def _relay(socket_path: str, arguments: Any) -> str:
    """Send one call to the runtime and wait for its answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path)
        connection.sendall((json.dumps({"input": arguments}) + "\n").encode("utf-8"))
        received = b""
        while not received.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
    if not received:
        raise ConnectionError("runtime closed the call without an answer")
    return str(json.loads(received.decode("utf-8"))["text"])


def serve(socket_path: str) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, message_id = message.get("method"), message.get("id")
        if method == "initialize":
            _reply(message_id, {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "alx", "version": "1"},
            })
        elif method == "tools/list":
            _reply(message_id, {"tools": [TOOL]})
        elif method == "tools/call":
            params = message.get("params", {})
            if params.get("name") != TOOL_NAME:
                _error(message_id, -32602, "unknown tool")
                continue
            try:
                text = _relay(socket_path, params.get("arguments", {}))
            except (OSError, ValueError, KeyError) as error:
                _reply(message_id, {"content": [{"type": "text", "text": (
                    f"The runtime did not answer this call ({type(error).__name__}).")}],
                    "isError": True})
                continue
            _reply(message_id, {"content": [{"type": "text", "text": text}]})
        elif message_id is not None:
            _reply(message_id, {})


if __name__ == "__main__":
    serve(sys.argv[1])
