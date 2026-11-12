"""Relay the Core's native tool calls from the Claude CLI to the waiting runtime.

The Claude CLI starts this as a stdio MCP server for one reasoning session. It
offers exactly the tools the runtime wrote to the file it is given, and decides
nothing about them: each call is written to the runtime's socket with the
tool's name, and whatever the runtime answers is returned to the model as the
tool result. Which step may run, whether it is safe, and what its result means
all stay with the Core, which handles each native step on its one path (D-065).

Calls are answered one at a time, in the order they arrive, because the Core
handles one step at a time.

Standard library only: it runs in the CLI's process tree, not the runtime's.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any


def _reply(message_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(message_id: Any, code: int, text: str) -> None:
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text},
    }) + "\n")
    sys.stdout.flush()


def _relay(socket_path: str, name: str, arguments: Any) -> list[dict[str, Any]]:
    """Send one call to the runtime and wait for its answer, as MCP content.

    The answer is its text and, when the call was to look at something
    (D-075), the pictures the runtime attached, each passed on as it came.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path)
        connection.sendall((json.dumps({"name": name, "input": arguments}) + "\n").encode("utf-8"))
        received = b""
        while not received.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
    if not received:
        raise ConnectionError("runtime closed the call without an answer")
    answer = json.loads(received.decode("utf-8"))
    content: list[dict[str, Any]] = [{"type": "text", "text": str(answer["text"])}]
    for image in answer.get("images") or ():
        content.append({"type": "image", "data": str(image["data"]),
                        "mimeType": str(image["mimeType"])})
    return content


def serve(socket_path: str, tools_path: str) -> None:
    with open(tools_path, encoding="utf-8") as handle:
        tools = json.load(handle)
    names = {item["name"] for item in tools}
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
            _reply(message_id, {"tools": tools})
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            if name not in names:
                _error(message_id, -32602, "unknown tool")
                continue
            try:
                content = _relay(socket_path, name, params.get("arguments", {}))
            except (OSError, ValueError, KeyError) as error:
                _reply(message_id, {"content": [{"type": "text", "text": (
                    f"The runtime did not answer this call ({type(error).__name__}).")}],
                    "isError": True})
                continue
            _reply(message_id, {"content": content})
        elif message_id is not None:
            _reply(message_id, {})


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2])
