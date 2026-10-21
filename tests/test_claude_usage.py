"""Screen protocol and real PTYs with fake executables only; no Claude login."""

import os
import re
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import CapabilityCall, CapabilityResultState, SideEffect  # noqa: E402
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402
from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel  # noqa: E402
from alx.providers.claude_usage import ClaudeUsageReader, parse_usage, rendered_screen  # noqa: E402
from alx.tools.claude_usage import DEFINITION, build_claude_usage_executors  # noqa: E402

SCREEN = (ROOT / 'tests/fixtures/claude_usage.txt').read_text()
TRUST_SCREEN = """ Accessing workspace:

 /private/var/folders/.../alx-claude-usage-XXXX

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source project, or work from your team). If not, take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""
TRUST_YES_SCREEN = TRUST_SCREEN.replace('❯ No, exit', '  No, exit').replace(
    '  Yes, I trust this folder', '❯ Yes, I trust this folder')


class GovernanceTests(unittest.TestCase):
    def test_usage_sequence_has_recorded_approval_and_registration_reference(self):
        decisions = (ROOT / 'governance/DECISIONS.md').read_text()
        records = re.split(r'^## ', decisions, flags=re.MULTILINE)[1:]
        approved = [record for record in records
                    if '`read_claude_usage`' in record
                    and '**Status: APPROVED by Friedl, 2026-10-20.**' in record]
        self.assertEqual(len(approved), 1,
                         'the usage sequence needs its own recorded approval')
        record = approved[0]
        self.assertIn('- **Date:** 2026-10-20', record)
        self.assertIn('- **Decision owner:** Friedl', record)
        decision_id = record.split(' — ', 1)[0]
        registration = (ROOT / 'src/alx/bootstrap/live_voice.py').read_text()
        self.assertIn(f'# {decision_id}:', registration)


class ParsingTests(unittest.TestCase):
    def test_multiple_limits_and_exact_reset_text(self):
        values = parse_usage(SCREEN)
        self.assertNotIn('failure_code', values)
        self.assertEqual(values['limits'], [
            {'label': 'Current session', 'percent_used': 12.0,
             'resets_at': '3pm (Africa/Johannesburg)'},
            {'label': 'Current week (all models)', 'percent_used': 25.0,
             'resets_at': 'Oct 23 at 9am (Africa/Johannesburg)'},
            {'label': 'Current week (Sonnet only)', 'percent_used': 4.5,
             'resets_at': 'Oct 24 at 2:30pm (Africa/Johannesburg)'},
        ])
        self.assertIsNotNone(datetime.fromisoformat(values['read_at']).utcoffset())

    def test_ansi_and_redraws(self):
        decorated = '\x1b]0;Claude\x07\x1b[?25l\x1b[31mstale\x1b[0m\x1b[2J\x1b[H' + SCREEN
        decorated = decorated.replace('12% used', '\x1b[32m12% used\x1b[0m')
        self.assertEqual(parse_usage(decorated)['limits'], parse_usage(SCREEN)['limits'])
        self.assertNotIn('\x1b', parse_usage(decorated)['screen_capture'])
        self.assertEqual(rendered_screen('99% used\r\x1b[2K12% used'), '12% used')
        self.assertEqual(rendered_screen('old\nwrong\x1b[1A\r\x1b[2Knew'), 'new\nwrong')

    def test_unreadable_incomplete_ambiguous_and_unknown_limits_fail(self):
        for screen in ('Please sign in', SCREEN.replace('12% used', '101% used'),
                       SCREEN.replace('12% used', '-1% used'),
                       SCREEN.replace('12% used', '12% spent'),
                       '\x1b[1S' + SCREEN,
                       SCREEN.replace('Resets 3pm', 'Resets ' + 'x' * 250),
                       SCREEN.replace('Resets 3pm (Africa/Johannesburg)', ''),
                       SCREEN.replace('12% used', '12% used 14% used'),
                       SCREEN.replace('Esc to cancel', ''),
                       SCREEN + '\nUnknown limit\n15% used\nResets tomorrow'):
            with self.subTest(screen=screen):
                result = parse_usage(screen)
                self.assertEqual(result['failure_code'], 'usage_unreadable')
                self.assertEqual(result['limits'], [])
                self.assertTrue(result['screen_capture'])

    def test_one_argument_free_read_only_registration_and_failure_evidence(self):
        registry = CapabilityRegistry((DEFINITION,))
        definition = registry.lookup('read_claude_usage')
        self.assertEqual(definition.side_effect, SideEffect.NONE)
        self.assertTrue(definition.input_schema.accepts({}))
        self.assertFalse(definition.input_schema.accepts({'text': 'usage'}))
        for screen, state in ((SCREEN, CapabilityResultState.SUCCEEDED),
                              ('unreadable', CapabilityResultState.FAILED)):
            executor = build_claude_usage_executors(lambda: parse_usage(screen), lambda: 'call')
            self.assertEqual(list(executor), ['read_claude_usage'])
            result = executor['read_claude_usage']({})
            self.assertEqual(result.state, state)
            self.assertEqual(result.values['screen_capture'], screen.strip())
            self.assertTrue(definition.output_schema.accepts(result.values))
            if state is CapabilityResultState.FAILED:
                self.assertEqual(result.failure['code'], 'usage_unreadable')

    def test_broker_rejects_extra_arguments_before_reading(self):
        calls = []
        def read():
            calls.append(True)
            return parse_usage(SCREEN)
        broker = CapabilityBroker(
            CapabilityRegistry((DEFINITION,)),
            SafetyGate({'read_claude_usage': AuthorityPolicy(frozenset({'self.read'}))}),
            build_claude_usage_executors(read, lambda: 'call'),
        )
        authority = AuthorityContext('alx', frozenset({'self.read'}), datetime.now(UTC))
        invalid = broker.dispatch(CapabilityCall('call', 'read_claude_usage', {'text': 'usage'}), authority)
        self.assertEqual(invalid.reason_code, 'input_invalid')
        self.assertEqual(calls, [])
        valid = broker.dispatch(CapabilityCall('call', 'read_claude_usage', {}), authority)
        self.assertEqual(valid.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(calls, [True])


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cli = self.root / 'claude'
        self.pid_file = self.root / 'pid'
        self.events = self.root / 'events'

    def fake(self, behavior, *, prelude=''):
        source = (
            '#!' + sys.executable + '\n' + prelude +
            'import os, sys, time, tty, signal\n'
            'from pathlib import Path\n'
            f'Path({str(self.pid_file)!r}).write_text(str(os.getpid()))\n'
            f'events = Path({str(self.events)!r})\n'
            'assert os.isatty(0) and os.isatty(1)\n'
            'tty.setraw(0)\n' + behavior
        )
        self.cli.write_text(source)
        self.cli.chmod(0o700)

    def reader(self, timeout=2):
        model = ClaudeSubscriptionReasoningModel(
            'model', 30, executable=str(self.cli),
            environment={**os.environ, 'ANTHROPIC_API_KEY': 'must-not-inherit',
                         'CLAUDE_CODE_OAUTH_TOKEN': 'wrong-login'},
            config_dir=str(self.root / 'dedicated'), expected_account='alx@example.test',
        )
        return ClaudeUsageReader(model.executable, model.child_environment(), timeout_seconds=timeout)

    def assert_dead(self):
        pid = int(self.pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_interactive_usage_same_identity_and_reaped_exit(self):
        self.fake(
            f'assert os.environ["CLAUDE_CONFIG_DIR"] == {str(self.root / "dedicated")!r}\n'
            'assert "ANTHROPIC_API_KEY" not in os.environ\n'
            'assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ\n'
            'assert "--print" not in sys.argv and "-p" not in sys.argv\n'
            'os.write(1, \'❯ Try "write a test"\'.encode())\n'
            'command = b""\n'
            'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
            'assert command == b"/usage\\r"\n'
            f'os.write(1, {("\x1b[2J\x1b[H" + SCREEN).encode()!r})\n'
            'while True:\n'
            '    data = os.read(0, 1024)\n'
            '    with events.open("ab") as log: log.write(data)\n'
        )
        values = self.reader().read()
        self.assertNotIn('failure_code', values)
        self.assertEqual(values['limits'], parse_usage(SCREEN)['limits'])
        self.assertFalse(self.events.exists(), 'cleanup must not send confirmation keys')
        self.assert_dead()

    def test_timeout_kills_and_reaps_uncooperative_child(self):
        # Emit evidence before fixture setup, with startup headroom under load.
        self.fake('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                  'while True: time.sleep(1)\n',
                  prelude='import os\nos.write(1, b"still starting")\n')
        started = time.monotonic()
        values = self.reader(3).read()
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(values['failure_code'], 'usage_timeout')
        self.assertIn('still starting', values['screen_capture'])
        self.assert_dead()

    def test_only_own_disposable_workspace_trust_is_confirmed(self):
        self.fake(
            'os.write(1, (os.getcwd() + "\\n❯ 1. Yes, I trust this folder\\n").encode())\n'
            'command = b""\n'
            'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
            'assert command == b"\\r"\n'
            'os.write(1, b"> ")\n'
            'command = b""\n'
            'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
            'assert command == b"/usage\\r"\n'
            f'os.write(1, {SCREEN.encode()!r})\n'
            'while True: time.sleep(1)\n'
        )
        self.assertNotIn('failure_code', self.reader().read())
        self.assert_dead()

        self.fake('os.write(1, b"/some/other/folder\\n1. Yes, I trust this folder\\n")\n'
                  'data = os.read(0, 1024)\nevents.write_bytes(data)\n'
                  'while True: time.sleep(1)\n')
        self.assertEqual(self.reader(1).read()['failure_code'], 'usage_unreadable')
        # An unconfirmed trust screen must never receive Enter, even for cleanup.
        if self.events.exists():
            self.assertNotIn(b'\r', self.events.read_bytes())
        self.assert_dead()

    def test_folder_trust_prompt_precedes_usage(self):
        for question in ('Do you trust this folder?',
                         'Do you trust the files in this folder?'):
            with self.subTest(question=question):
                self.fake(
                    f'os.write(1, ({question!r} + "\\n" + os.getcwd() + '
                    '"\\n  1. Yes, proceed\\n❯ 2. No, exit\\n").encode())\n'
                    'assert os.read(0, 3) == b"\\x1b[B"\n'
                    'os.write(1, ("\\x1b[2J\\x1b[H" + os.getcwd() + '
                    '"\\n❯ 1. Yes, proceed\\n  2. No, exit\\n").encode())\n'
                    'command = b""\n'
                    'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
                    'assert command == b"\\r"\n'
                    'os.write(1, b"> ")\n'
                    'command = b""\n'
                    'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
                    'assert command == b"/usage\\r"\n'
                    f'os.write(1, {SCREEN.encode()!r})\n'
                    'while True: time.sleep(1)\n'
                )
                values = self.reader().read()
                self.assertNotIn('failure_code', values)
                self.assertEqual(values['limits'], parse_usage(SCREEN)['limits'])
                self.assert_dead()

    def test_quick_safety_check_requires_confirmed_yes_selection(self):
        for initially_yes, moves in ((False, True), (True, True), (False, False)):
            with self.subTest(initially_yes=initially_yes, moves=moves):
                initial = TRUST_YES_SCREEN if initially_yes else TRUST_SCREEN
                self.fake(
                    f'screen = {initial!r}.replace("/private/var/folders/.../alx-claude-usage-XXXX", os.getcwd())\n'
                    'os.write(1, screen.encode())\n'
                    + ('' if initially_yes else
                       'assert os.read(0, 3) == b"\\x1b[B"\n'
                       + (f'screen = {TRUST_YES_SCREEN!r}.replace("/private/var/folders/.../alx-claude-usage-XXXX", os.getcwd())\n'
                          if moves else '')
                       + 'os.write(1, ("\\x1b[2J\\x1b[H" + screen).encode())\n')
                    + ('command = b""\n'
                       'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
                       'assert command == b"\\r"\n'
                       'events.write_text("confirmed_yes")\n'
                       'os.write(1, b"\\x1b[2J\\x1b[H> ")\n'
                       'command = b""\n'
                       'while not command.endswith(b"\\r"): command += os.read(0, 1)\n'
                       'assert command == b"/usage\\r"\n'
                       f'os.write(1, {("\x1b[2J\x1b[H" + SCREEN).encode()!r})\n'
                       'while True: time.sleep(1)\n'
                       if moves else
                       'while True:\n'
                       '    data = os.read(0, 1024)\n'
                       '    with events.open("ab") as log: log.write(data)\n')
                )
                self.events.unlink(missing_ok=True)
                values = self.reader().read()
                if moves:
                    self.assertNotIn('failure_code', values)
                    self.assertEqual(values['limits'], parse_usage(SCREEN)['limits'])
                    self.assertEqual(self.events.read_text(), 'confirmed_yes')
                else:
                    self.assertEqual(values['failure_code'], 'usage_unreadable')
                    self.assertEqual(values['limits'], [])
                    self.assertIn('❯ No, exit', values['screen_capture'])
                    self.assertFalse(self.events.exists(), 'no input after unconfirmed selection')
                self.assert_dead()

    def test_timeout_after_usage_and_unreadable_screen(self):
        for output, expected in (('loading', 'usage_timeout'),
                                 ('unreadable\nEsc to cancel', 'usage_unreadable')):
            self.fake('os.write(1, b"> ")\n'
                      'while os.read(0, 1) != b"\\r": pass\n'
                      f'os.write(1, {output.encode()!r})\n'
                      'while True: time.sleep(1)\n')
            values = self.reader(0.9).read()
            self.assertEqual(values['failure_code'], expected)
            self.assertIn(output, values['screen_capture'])
            self.assert_dead()

    def test_reader_protocol_exception_still_reaps_child(self):
        self.fake('os.write(1, b"> ")\n'
                  'while os.read(0, 1) != b"\\r": pass\n'
                  f'os.write(1, {SCREEN.encode()!r})\n'
                  'while True: time.sleep(1)\n')
        with patch('alx.providers.claude_usage.parse_usage', side_effect=RuntimeError('parser failed')):
            with self.assertRaisesRegex(RuntimeError, 'parser failed'):
                self.reader().read()
        self.assert_dead()

    def test_missing_executable_and_spawn_error_close_both_descriptors(self):
        import alx.providers.coding_process as runner
        for spawn in (None, OSError('spawn failed')):
            with patch.object(runner.os, 'close', wraps=os.close) as close:
                if spawn:
                    with patch.object(runner.subprocess, 'Popen', side_effect=spawn):
                        values = self.reader().read()
                else:
                    values = self.reader().read()
                self.assertEqual(values['failure_code'], 'cli_unavailable')
                self.assertGreaterEqual(close.call_count, 2)
                self.assertEqual(values['screen_capture'], '')


class OutputDrivenTests(unittest.TestCase):
    """Virtual PTY time makes delayed redraws independent of host load."""

    def drive(self, initial, transitions, timeout=4):
        from contextlib import contextmanager
        from types import SimpleNamespace
        import alx.providers.claude_usage as usage

        now, pending, keys, closed = [0.0], [], [], []

        @contextmanager
        def terminal(*args, **kwargs):
            pending.extend((delay, text.replace('{cwd}', kwargs['cwd']).encode())
                           for delay, text in initial)
            try:
                yield 999, SimpleNamespace(poll=lambda: None)
            finally:
                closed.append(True)

        def select_output(read, write, error, timeout):
            pending.sort(key=lambda item: item[0])
            if pending and pending[0][0] <= now[0] + timeout:
                now[0] = max(now[0], pending[0][0])
                return read, [], []
            now[0] += timeout
            return [], [], []

        def send(fd, data):
            keys.append((now[0], data))
            pending.extend((now[0] + delay, text.encode())
                           for delay, text in transitions.get(data, []))
            return len(data)

        with patch.object(usage, 'interactive_terminal', terminal), \
             patch.object(usage, 'monotonic', lambda: now[0]), \
             patch.object(usage.select, 'select', select_output), \
             patch.object(usage.os, 'read', lambda *args: pending.pop(0)[1]), \
             patch.object(usage.os, 'write', send):
            result = ClaudeUsageReader('unused', {}, timeout_seconds=timeout).read()
        self.assertEqual(closed, [True])
        self.assertLessEqual(now[0], timeout)
        return result, keys

    def test_delayed_down_redraw_review_regression(self):
        initial = TRUST_SCREEN.replace('/private/var/folders/.../alx-claude-usage-XXXX', '{cwd}')
        yes = '\x1b[11;1H\x1b[2K   No, exit\x1b[12;1H\x1b[2K ❯ Yes, I trust this folder'
        result, keys = self.drive([(0, initial)], {
            b'\x1b[B': [(0.4, yes)],
            b'\r': [(0.6, '\x1b[2J\x1b[H> ')],
            b'/usage\r': [(0.8, '\x1b[2J\x1b[H' + SCREEN)],
        })
        self.assertNotIn('failure_code', result)
        self.assertEqual([key for _, key in keys], [b'\x1b[B', b'\r', b'/usage\r'])
        self.assertGreaterEqual(keys[1][0] - keys[0][0], 0.4)
        self.assertGreaterEqual(keys[2][0] - keys[1][0], 0.6)

    def test_usage_echo_and_partial_limits_do_not_complete_read(self):
        result, keys = self.drive([(0, '> ')], {
            b'/usage\r': [(0.01, '/usage\r\n> '),
                           (0.8, '\x1b[2J\x1b[H' + SCREEN.replace('25% used', 'loading')),
                           (1.6, '\x1b[2J\x1b[H' + SCREEN)],
        })
        self.assertNotIn('failure_code', result)
        self.assertEqual(result['limits'], parse_usage(SCREEN)['limits'])
        self.assertEqual([key for _, key in keys], [b'/usage\r'])

    def test_unknown_interstitial_is_evidence_without_confirmation(self):
        for screen in ('Choose a theme\n❯ Dark', 'Sign in\n❯ Continue',
                       'Update available\n❯ Install', 'Important notice\n> Continue'):
            with self.subTest(screen=screen):
                result, keys = self.drive([(0, screen)], {})
                self.assertEqual(result['failure_code'], 'usage_unreadable')
                self.assertIn(screen, result['screen_capture'])
                self.assertEqual(keys, [])

    def test_down_without_selected_yes_never_confirms(self):
        initial = TRUST_SCREEN.replace('/private/var/folders/.../alx-claude-usage-XXXX', '{cwd}')
        result, keys = self.drive([(0, initial)], {b'\x1b[B': [(0.4, '\x1b[0m')]})
        self.assertEqual(result['failure_code'], 'usage_unreadable')
        self.assertEqual([key for _, key in keys], [b'\x1b[B'])

    def test_fragmented_ansi_redraw_cannot_authorize_enter_early(self):
        initial = TRUST_SCREEN.replace('/private/var/folders/.../alx-claude-usage-XXXX', '{cwd}')
        result, keys = self.drive([(0, initial)], {
            b'\x1b[B': [(0.2, '\x1b[11;1H\x1b[2K   No, exit\x1b[12;1H\x1b[2K ❯ Yes, I trust this folder\x1b['),
                         (0.7, '0m')],
            b'\r': [(0.5, '\x1b[2J\x1b[H> ')],
            b'/usage\r': [(0.1, '\x1b[2J\x1b[H' + SCREEN)],
        })
        self.assertNotIn('failure_code', result)
        self.assertGreaterEqual(keys[1][0], 0.7)

    def test_confirmed_trust_waits_for_prompt_not_another_interstitial(self):
        initial = TRUST_YES_SCREEN.replace('/private/var/folders/.../alx-claude-usage-XXXX', '{cwd}')
        result, keys = self.drive([(0, initial)], {
            b'\r': [(0.5, '\x1b[2J\x1b[HLogin required\n❯ Sign in')],
        })
        self.assertEqual(result['failure_code'], 'usage_unreadable')
        self.assertIn('Login required', result['screen_capture'])
        self.assertEqual([key for _, key in keys], [b'\r'])

    def test_footer_before_incomplete_limits_and_silent_transitions(self):
        for emissions, expected in (
            ([], 'usage_timeout'),
            ([(0.1, 'Esc to cancel\nCurrent session\n12% used\nResets soon\n'
                     'Current week (all models)\nloading')], 'usage_unreadable'),
        ):
            with self.subTest(emissions=emissions):
                result, keys = self.drive([(0, '> ')], {b'/usage\r': emissions})
                self.assertEqual(result['failure_code'], expected)
                self.assertEqual([key for _, key in keys], [b'/usage\r'])

    def test_silent_startup_is_bounded(self):
        result, keys = self.drive([], {})
        self.assertEqual(result['failure_code'], 'usage_timeout')
        self.assertEqual(keys, [])

    def test_footer_painted_first_does_not_finish_partial_panel(self):
        result, keys = self.drive([(0, '> ')], {
            b'/usage\r': [(0.1, 'Esc to cancel\nCurrent session\n12% used\nResets soon\n'
                           'Current week (all models)\n25% used\nResets tomorrow'),
                          (1.2, '\x1b[2J\x1b[H' + SCREEN)],
        })
        self.assertNotIn('failure_code', result)
        self.assertEqual(result['limits'], parse_usage(SCREEN)['limits'])


class InteractiveTerminalTests(unittest.TestCase):
    def test_protocol_error_reaps_child_and_closes_terminal(self):
        from alx.providers.coding_process import interactive_terminal

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'protocol failed'):
                with interactive_terminal(
                    [sys.executable, '-c', 'import time; time.sleep(30)'],
                    cwd=directory, env=dict(os.environ), rows=80, columns=240,
                    deadline=time.monotonic() + 2,
                ) as (master, child):
                    self.assertEqual(os.get_terminal_size(master), (240, 80))
                    raise RuntimeError('protocol failed')
            with self.assertRaises(OSError):
                os.fstat(master)
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_terminal_setup_error_closes_both_descriptors_without_spawning(self):
        import alx.providers.coding_process as runner

        descriptors = runner.pty.openpty()
        with patch.object(runner.pty, 'openpty', return_value=descriptors):
            with patch.object(runner.fcntl, 'ioctl', side_effect=OSError('setup failed')):
                with patch.object(runner.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(OSError, 'setup failed'):
                        with runner.interactive_terminal(
                            ['unused'], cwd='.', env={}, rows=80, columns=240,
                            deadline=time.monotonic() + 2,
                        ):
                            self.fail('terminal setup should fail')
                    spawn.assert_not_called()
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)


if __name__ == '__main__':
    unittest.main()
