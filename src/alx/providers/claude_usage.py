"""Read the subscription's interactive /usage screen without a model turn."""

from __future__ import annotations

import codecs
import errno
import os
import re
import select
from collections.abc import Mapping
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from alx.providers.coding_process import interactive_terminal

_ROWS, _COLUMNS = 80, 240
_MAX_CAPTURE = 256_000
_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b(?![\[\]])[@-_]")
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
            if char == '\x1b':
                unsupported = True
            elif char == '\n':
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
    footers = [i for i, line in enumerate(screen_lines) if _FOOTER.search(line)]
    try:
        if (not headings or len(footers) != 1 or footers[0] <= headings[-1][0]
                or '[terminal_output_unreadable]' in screen or '\x1b' in screen):
            raise ValueError
        for index, (start, label) in enumerate(headings):
            end = headings[index + 1][0] if index + 1 < len(headings) else footers[0]
            block = '\n'.join(screen_lines[start + 1:end])
            percents, resets = _PERCENT.findall(block), _RESET.findall(block)
            if len(percents) != 1 or len(resets) != 1 or not 0 <= float(percents[0]) <= 100:
                raise ValueError
            if any(item['label'] == label for item in values['limits']):
                raise ValueError
            values['limits'].append({
                'label': label, 'percent_used': float(percents[0]), 'resets_at': resets[0],
            })
        if (screen.count('%') != len(headings)
                or len(_RESET.findall('\n'.join(screen_lines))) != len(headings)):
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
        child = None
        code = 'usage_timeout'
        sent = trusted = trust_pending = trust_moved = False
        screen = ''
        selected: list[str] = []
        try:
            with TemporaryDirectory(prefix='alx-claude-usage-') as directory:
                with interactive_terminal(
                    [self.executable, '--tools', '', '--mcp-config', '{"mcpServers":{}}',
                     '--strict-mcp-config', '--setting-sources', ''],
                    cwd=directory,
                    env={**self.environment, 'TERM': 'xterm-256color'},
                    rows=_ROWS, columns=_COLUMNS, deadline=deadline,
                ) as (master, child):
                    while monotonic() < deadline:
                        ready, _, _ = select.select(
                            [master], [], [], max(0, deadline - monotonic()),
                        )
                        # No output means no new evidence about the last key.
                        if not ready:
                            continue
                        try:
                            chunk = os.read(master, 16384)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            chunk = b''
                        if not chunk:
                            code = 'usage_unreadable'
                            break
                        capture += decoder.decode(chunk)
                        if len(capture) > _MAX_CAPTURE:
                            capture = capture[:_MAX_CAPTURE]
                            code = 'usage_unreadable'
                            break
                        screen = rendered_screen(capture)
                        # Never interpret an incomplete escape or an unsupported
                        # cursor operation as a selection or command prompt.
                        if '\x1b' in screen or '[terminal_output_unreadable]' in screen:
                            continue
                        selected = [line.strip() for line in screen.splitlines()
                                    if line.lstrip().startswith(('❯', '>'))]
                        is_trust = (
                            '1. Yes, I trust this folder' in screen
                            or ('1. Yes, proceed' in screen and (
                                'Do you trust this folder?' in screen
                                or 'Do you trust the files in this folder?' in screen))
                            or ('Quick safety check:' in screen
                                and 'Yes, I trust this folder' in screen
                                and 'No, exit' in screen)
                        )
                        if not trusted and not sent and (trust_pending or is_trust):
                            trust_pending = True
                            # Only this disposable directory may be trusted.
                            # A partial redraw may temporarily omit its path.
                            paths = (directory, os.path.realpath(directory))
                            if not any(line.strip() in paths for line in screen.splitlines()):
                                continue
                            yes = len(selected) == 1 and re.fullmatch(
                                r'[❯>]\s*(?:1\.\s*)?Yes, (?:I trust this folder|proceed)',
                                selected[0],
                            )
                            no = len(selected) == 1 and re.fullmatch(
                                r'[❯>]\s*(?:2\.\s*)?No, exit', selected[0],
                            )
                            if yes:
                                os.write(master, b'\r')
                                trusted, trust_pending = True, False
                                # Retain the renderer's cursor state after Down,
                                # but start a fresh evidence frame after Enter.
                                capture = ''
                                screen = ''
                            elif no and not trust_moved:
                                os.write(master, b'\x1b[B')
                                trust_moved = True
                            # An echo, unchanged No, or partial redraw is not
                            # failure while time remains, and never permits Enter.
                        elif not sent and len(selected) == 1 and _PROMPT.fullmatch(
                            selected[0] + '\n'
                        ):
                            os.write(master, b'/usage\r')
                            capture = ''
                            screen = ''
                            sent = True
                        elif sent:
                            values = parse_usage(capture)
                            labels = [item['label'].lower() for item in values['limits']]
                            # Both session and weekly limits must be present;
                            # a footer alone or a command echo is not completion.
                            if ('failure_code' not in values
                                    and 'current session' in labels
                                    and any(label.startswith(('current week', 'weekly'))
                                            for label in labels)):
                                code = ''
                                break
                    else:
                        # Preserve the last screen as evidence. Unsupported
                        # selections and incomplete usage panels are unreadable;
                        # a silent CLI/loading screen remains a timeout.
                        if trust_pending or (not sent and selected) or (
                            sent and _FOOTER.search(screen)
                        ):
                            code = 'usage_unreadable'
                    # interactive_terminal owns termination and reaping on all
                    # paths. No cleanup keys may confirm an interstitial screen.
        except OSError:
            code = 'cli_unavailable' if child is None else 'usage_unreadable'
        values = parse_usage(capture)
        if code:
            values.update(limits=[], failure_code=code)
        return values
