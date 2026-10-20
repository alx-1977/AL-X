"""Read the subscription's interactive /usage screen without a model turn."""

from __future__ import annotations

import codecs
import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
from collections.abc import Mapping
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

_ROWS, _COLUMNS = 80, 240
_MAX_CAPTURE = 256_000
_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_LABEL = re.compile(r"(?:Current session|Current week(?:\s*\([^\n]+\))?|Weekly(?:\s+[^\n]+)?)", re.I)
_PERCENT = re.compile(r"(?<![\d.+-])(\d+(?:\.\d+)?)%\s*used\b", re.I)
_RESET = re.compile(r"^Resets\s+(.+)$", re.I | re.M)
_FOOTER = re.compile(r"Esc to (?:cancel|close|exit)", re.I)
_PROMPT = re.compile(r'(?:^|\n)[ \t]*[❯>][ \t]*(?:Try ["“][^\n]*)?(?:\n|$)')


def rendered_screen(capture: str) -> str:
    """Apply cursor/erase controls so redraws cannot become duplicate limits."""
    lines: list[list[str]] = [[] for _ in range(_ROWS)]
    row = column = offset = 0
    clipped = False
    unsupported = False
    # The sentinel ensures trailing plain text passes through the same loop.
    for match in _ESCAPE.finditer(capture + '\x1b[m'):
        for char in capture[offset:match.start()]:
            if char == '\n':
                clipped |= row == _ROWS - 1
                row = min(row + 1, _ROWS - 1)
                column = 0
            elif char == '\r':
                column = 0
            elif char == '\b':
                column = max(0, column - 1)
            elif char >= ' ' and char != '\x7f' and column < _COLUMNS:
                line = lines[row]
                line.extend(' ' for _ in range(column + 1 - len(line)))
                line[column] = char
                column += 1
            elif char >= ' ' and char != '\x7f':
                clipped = True
        token = match.group()
        if token.startswith('\x1b['):
            body, op = token[2:-1], token[-1]
            if all(part.isdigit() or not part for part in body.split(';')):
                args = [int(part or 0) for part in body.split(';')]
                n = args[0] or 1
                if op in ('H', 'f'):
                    row = min(n - 1, _ROWS - 1)
                    column = min((args[1] or 1) - 1, _COLUMNS - 1) if len(args) > 1 else 0
                elif op == 'A':
                    row = max(0, row - n)
                elif op == 'B':
                    row = min(_ROWS - 1, row + n)
                elif op == 'C':
                    column = min(_COLUMNS - 1, column + n)
                elif op == 'D':
                    column = max(0, column - n)
                elif op == 'G':
                    column = min(_COLUMNS - 1, n - 1)
                elif op == 'd':
                    row = min(_ROWS - 1, n - 1)
                elif op == 'E':
                    row, column = min(_ROWS - 1, row + n), 0
                elif op == 'F':
                    row, column = max(0, row - n), 0
                elif op == 'J':
                    if args[0] in (2, 3):
                        lines = [[] for _ in range(_ROWS)]
                    elif args[0] == 0:
                        lines[row] = lines[row][:column]
                        lines[row + 1:] = [[] for _ in range(_ROWS - row - 1)]
                elif op == 'K':
                    if args[0] == 0:
                        lines[row] = lines[row][:column]
                    elif args[0] == 2:
                        lines[row] = []
                    elif args[0] == 1:
                        lines[row][:column + 1] = [' '] * min(column + 1, len(lines[row]))
                elif op not in ('m', 'h', 'l', 'n', 'c', 't'):
                    unsupported = True
            elif op not in ('m', 'h', 'l', 'q'):
                unsupported = True
        offset = match.end()
    cleaned_screen = '\n'.join(''.join(line).rstrip() for line in lines).strip()
    if clipped or unsupported:
        cleaned_screen += '\n[terminal_output_unreadable]'
    return cleaned_screen


def parse_usage(screen_capture: str) -> dict[str, Any]:
    """Return complete printed values or a structured failure, never estimates."""
    screen = rendered_screen(screen_capture)
    values: dict[str, Any] = {
        'limits': [], 'screen_capture': screen, 'read_at': datetime.now(UTC).isoformat(),
    }
    screen_lines = [line.strip().strip('│').strip() for line in screen.splitlines()]
    headings = [(i, line) for i, line in enumerate(screen_lines) if _LABEL.fullmatch(line)]
    try:
        if not headings or not _FOOTER.search(screen) or '[terminal_output_unreadable]' in screen or '\x1b' in screen:
            raise ValueError
        for index, (start, label) in enumerate(headings):
            end = headings[index + 1][0] if index + 1 < len(headings) else len(screen_lines)
            block = '\n'.join(screen_lines[start + 1:end])
            percents, resets = _PERCENT.findall(block), _RESET.findall(block)
            if len(percents) != 1 or len(resets) != 1 or not 0 <= float(percents[0]) <= 100:
                raise ValueError
            if any(item['label'] == label for item in values['limits']):
                raise ValueError
            values['limits'].append({
                'label': label, 'percent_used': float(percents[0]), 'resets_at': resets[0],
            })
        if screen.count('%') != len(headings):
            raise ValueError
    except ValueError:
        values['limits'] = []
        values['failure_code'] = 'usage_unreadable'
    return values


class ClaudeUsageReader:
    def __init__(self, executable: str, environment: Mapping[str, str], *, timeout_seconds: float = 20) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError('usage timeout must be between zero and 60 seconds')
        self.executable = executable
        self.environment = dict(environment)
        self.timeout_seconds = timeout_seconds

    def read(self) -> dict[str, Any]:
        deadline = monotonic() + self.timeout_seconds
        capture = ''
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        master = slave = None
        child = None
        code = 'usage_timeout'
        sent = False
        trusted = False
        last_output = monotonic()
        try:
            with TemporaryDirectory(prefix='alx-claude-usage-') as directory:
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', _ROWS, _COLUMNS, 0, 0))
                child = subprocess.Popen(
                    [self.executable, '--tools', '', '--mcp-config', '{"mcpServers":{}}',
                     '--strict-mcp-config', '--setting-sources', ''],
                    stdin=slave, stdout=slave, stderr=slave, cwd=directory,
                    env={**self.environment, 'TERM': 'xterm-256color'}, start_new_session=True,
                )
                os.close(slave)
                slave = None
                while monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], min(0.1, max(0, deadline - monotonic())))
                    if ready:
                        try:
                            chunk = os.read(master, 16384)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            chunk = b''
                        if not chunk:
                            code = '' if sent and _FOOTER.search(rendered_screen(capture)) else 'usage_unreadable'
                            break
                        capture += decoder.decode(chunk)
                        last_output = monotonic()
                        if len(capture) > _MAX_CAPTURE:
                            capture = capture[:_MAX_CAPTURE]
                            code = 'usage_unreadable'
                            break
                    screen = rendered_screen(capture)
                    # The CLI may ask to trust its workspace before showing a
                    # prompt. Only confirm our own empty disposable directory;
                    # never answer login, permission or onboarding questions.
                    if not trusted and not sent and (
                        '1. Yes, I trust this folder' in screen
                        and (directory in screen or os.path.realpath(directory) in screen)
                    ):
                        os.write(master, b'1\r')
                        trusted = True
                        capture = ''
                    elif not sent and _PROMPT.search(screen):
                        os.write(master, b'/usage\r')
                        capture = ''
                        sent = True
                        last_output = monotonic()
                    elif sent and monotonic() - last_output >= 0.5 and (
                        _FOOTER.search(screen) or _PROMPT.search(screen)
                    ):
                        code = ''
                        break
                if child.poll() is None:
                    os.write(master, b'\x1b')
                    os.write(master, b'\x15/exit\r')
                    try:
                        child.wait(timeout=max(0, min(0.3, deadline - monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
        except OSError:
            code = 'cli_unavailable' if child is None else 'usage_unreadable'
        finally:
            if child is not None:
                # An exited parent can still leave descendants behind.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            for descriptor in (master, slave):
                if descriptor is not None:
                    os.close(descriptor)
        values = parse_usage(capture)
        if code:
            values.update(limits=[], failure_code=code)
        return values
