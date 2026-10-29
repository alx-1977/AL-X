"""D-058: isolated board mechanics, bounded files and exact write authority."""
import json
import os
import pwd
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.config.settings import KiCadSettings, ConfigurationError
from alx.contracts import (Approval, ApprovalScope, ApprovalLifecycle, CapabilityCall,
                           CapabilityResultState, SideEffect)
from alx.core.model_reasoner import _catalogue_payload
from alx.providers.coding_process import kicad_containment_available as REAL_CONTAINMENT, run_kicad_cli
from alx.providers.kicad import KiCadProvider, KiCadError, inventory, board_facts, structural_diff
from alx.safety import AuthorityContext, SafetyGate
from alx.tools.kicad import DEFINITIONS


BOARD = '''(kicad_pcb
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "B.Fab" user) (50 "User.1" user))
  (gr_line (start 0 0) (end 1 1) (layer "B.Fab") (uuid "graphic-id"))
  (footprint "Test:Small"
    (uuid "footprint-id")
    (property "Reference" "U1" (layer "F.SilkS")
      (effects (font (size 0.5 0.7) (thickness 0.08))))
    (pad "1" smd rect (at 1 2) (size 1 1) (layers "F.Cu") (uuid "pad-id")))
  (gr_text "copper" (layer "F.Cu") (effects (font (size 0.4 0.4) (thickness 0.05))))
  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu")))'''
PROJECT = {'board': {'design_settings': {'rules': {'silk_text_height': 1,
    'silk_text_thickness': 0.15}, 'rule_severities': {'clearance': 'error'}}},
    'unrelated': {'keep': True}}


EDIT_SCRIPT = '''
import sys
from pathlib import Path
p = Path(sys.argv[1])
p.write_text(p.read_text().replace('(size 0.5 0.7)', '(size 1 1)').replace('(layer "B.Fab")', '(layer "User.1")'))
'''
TWO_FILE_SCRIPT = r'''
p.with_suffix('.kicad_pro').write_text(p.with_suffix('.kicad_pro').read_text() + '\n')
'''


class BoardFactsTests(unittest.TestCase):
    def test_layer_move_uses_direct_uuid_and_saved_layer_name(self):
        before = '''(kicad_pcb
          (layers (5 "F.SilkS" user "F.Silkscreen") (50 "User.1" user))
          (gr_line (start 0 0) (end 1 1)
            (layer "F.SilkS") (uuid "graphic-id"))
          (footprint "Test:Nested"
            (property "Reference" "U1" (layer "F.SilkS") (uuid "property-id"))
            (layer "F.Cu") (uuid "footprint-id")))'''
        after = '''(kicad_pcb
          (layers (5 "F.SilkS" user "F.Silkscreen") (50 "User.1" user))
          (footprint "Test:Nested"
            (property "Reference" "U1" (layer "F.SilkS") (uuid "property-id"))
            (layer "F.Cu") (uuid "footprint-id"))
          (gr_line (start 0 0) (end 1 1)
            (layer "User.1") (uuid "graphic-id")))'''
        left, right = board_facts(before), board_facts(after)
        self.assertEqual(left['items']['footprint-id']['layers'], ['F.Cu'])
        self.assertEqual(left['items']['property-id']['layers'], ['F.SilkS'])
        report = structural_diff(left, right)
        self.assertEqual(report['layer_moves'], [{
            'id': 'graphic-id', 'kind': 'gr_line',
            'before': ['F.SilkS'], 'after': ['User.1']}])
        self.assertEqual(report['items_added'], [])
        self.assertEqual(report['items_removed'], [])
        self.assertEqual(report['per_layer_before']['F.SilkS'], 2)
        self.assertEqual(report['per_layer_after']['F.SilkS'], 1)
        self.assertEqual(report['per_layer_after']['User.1'], 1)


class KiCadTests(unittest.TestCase):
    def setUp(self):
        # Unit tests replace the sandboxed runner, so they run on any OS;
        # the fail-closed check itself is tested against the real function.
        containment = patch('alx.providers.coding_process.kicad_containment_available', return_value=True)
        containment.start()
        self.addCleanup(containment.stop)
        self.temp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.design = self.root / 'designs'
        self.project = self.design / 'board'
        self.project.mkdir(parents=True)
        (self.project / 'test.kicad_pcb').write_text(BOARD)
        (self.project / 'test.kicad_pro').write_text(json.dumps(PROJECT))
        self.environment = {
            'ALX_KICAD_DESIGN_ROOTS': str(self.design),
            'ALX_KICAD_LIBRARY_ROOT': str(self.root / 'libraries'),
        }
        self.storage = self.root / 'storage'
        self.calls = []
        self.runtime = build_kicad_runtime(lambda: 'call', self.environment, self.storage, self.run_cli)
        self.settings = KiCadSettings.from_environment(self.environment, self.storage)

    def execute_cli(self, executable, arguments):
        return self.run_cli([executable, *arguments])

    def run_cli(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == '/usr/bin/sandbox-exec':
            import contextlib
            import io
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, 'argv', [argv[-2], argv[-1]]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(Path(argv[-2]).read_text(), argv[-2], 'exec'), {})
            return SimpleNamespace(returncode=0, stdout=out.getvalue(), stderr=err.getvalue())
        board = Path(argv[-1])
        self.assertTrue(board.is_relative_to(self.storage))
        self.assertFalse(board.is_relative_to(self.project))
        target = Path(argv[argv.index('--output') + 1])
        if argv[2] == 'drc' and getattr(self, 'report', None) is not None:
            target.write_text(json.dumps(self.report))
        elif argv[2] == 'drc':
            self.assertEqual(argv[3:7], ['--format', 'json', '--severity-all', '--refill-zones'])
            pro = json.loads(board.with_suffix('.kicad_pro').read_text())
            ignored = pro['board']['design_settings']['rule_severities'].get('lib_footprint_mismatch') == 'ignore'
            finding = {'type': 'lib_footprint_mismatch', 'severity': 'warning',
                       'description': 'Mismatch', 'items': [{'uuid': 'pad-id',
                       'description': 'Pad 1 of U1', 'pos': {'x': 1.0, 'y': 2.0}}]}
            target.write_text(json.dumps({'violations': [] if ignored else [finding],
                'unconnected_items': [dict(finding, type='unconnected_items', severity='error')],
                'schematic_parity': []}))
        else:
            self.assertEqual(argv[1:4], ['pcb', 'export', 'svg'])
            (target / 'front.svg').write_text('<svg/>')
        return SimpleNamespace(returncode=0)

    def call(self, name, **arguments):
        return self.runtime.executors[name](arguments)

    def success(self, name, **arguments):
        result = self.call(name, **arguments)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        return result.values

    def failure(self, code, name, **arguments):
        result = self.call(name, **arguments)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['code'], code)

    def check(self):
        return self.success('check_kicad_board', project_dir=str(self.project))

    def test_six_unique_catalogue_entries_and_one_runtime_registration(self):
        registry = CapabilityRegistry(self.runtime.definitions)
        self.assertEqual(len(registry.list_definitions()), 6)
        self.assertEqual(set(self.runtime.executors), {d.capability_id for d in DEFINITIONS})
        payload = json.loads(_catalogue_payload(self.runtime.definitions))
        self.assertEqual({d['id'] for d in payload['capabilities']}, set(self.runtime.executors))
        self.assertEqual([d.side_effect for d in DEFINITIONS],
                         [SideEffect.NONE, SideEffect.NONE, SideEffect.EFFECTFUL, SideEffect.NONE, SideEffect.NONE, SideEffect.EFFECTFUL])
        live = (Path(__file__).parents[1] / 'src/alx/bootstrap/live_voice.py').read_text()
        self.assertEqual(live.count('kicad_runtime = build_kicad_runtime('), 1)

    def test_write_back_catalogue_describes_partial_failure_recovery(self):
        payload = json.loads(_catalogue_payload(self.runtime.definitions))
        description = next(d['purpose'] for d in payload['capabilities']
                           if d['id'] == 'write_back_kicad_board')
        self.assertNotIn('rolls back on failure', description)
        self.assertIn('already replaced files may remain', description)
        self.assertIn('returned backups and evidence', description)
        self.assertIn('no automatic undo', description)

    def test_a_check_result_is_bounded_per_check_type(self):
        """D-057a: a full DRC report rode on every later Core call."""
        from alx.providers import kicad as provider
        def finding(kind, severity, index, items):
            return {'type': kind, 'severity': severity, 'description': f'{kind} {index}',
                    'items': [{'description': f'item {index}.{n}', 'pos': {'x': 1.0, 'y': 2.0}}
                              for n in range(items)]}
        real = provider.FINDINGS_PER_CHECK_TYPE + 3
        noise = provider.NOISE_EXAMPLES + 4
        self.report = {
            'violations': [finding('clearance', 'error', i, provider.ITEMS_PER_FINDING + 2)
                           for i in range(real)]
                          + [finding('lib_footprint_mismatch', 'warning', i, 1) for i in range(noise)],
            'unconnected_items': [], 'schematic_parity': []}
        result = self.check()
        groups = {group['check_type']: group for group in result['violations']}
        clearance, mismatch = groups['clearance'], groups['lib_footprint_mismatch']
        self.assertFalse(clearance['noise'])
        self.assertTrue(mismatch['noise'])
        self.assertEqual(clearance['count'], real)
        self.assertEqual([f['description'] for f in clearance['findings']],
                         [f'clearance {i}' for i in range(provider.FINDINGS_PER_CHECK_TYPE)])
        self.assertEqual(clearance['omitted_findings'], 3)
        self.assertEqual([i['description'] for i in clearance['findings'][0]['items']],
                         [f'item 0.{n}' for n in range(provider.ITEMS_PER_FINDING)])
        self.assertEqual(mismatch['count'], noise)
        self.assertEqual(len(mismatch['findings']), provider.NOISE_EXAMPLES)
        self.assertEqual(mismatch['omitted_findings'], 4)
        self.assertEqual(result['totals'],
                         {'error': real, 'warning': noise, 'ignore': 0, 'total': real + noise})

    def test_copy_only_grouping_and_restart(self):
        original = inventory(self.project)
        result = self.check()
        self.assertEqual(inventory(self.project), original)
        group = result['violations'][0]
        self.assertEqual((group['check_type'], group['count'], group['noise']),
                         ('lib_footprint_mismatch', 1, True))
        item = group['findings'][0]['items'][0]
        self.assertEqual((item['designator'], item['pad_number']), ('U1', '1'))
        self.assertEqual(item['position'], {'x': 1.0, 'y': 2.0})
        self.assertFalse(result['unconnected_items'][0]['noise'])
        self.assertEqual(result['totals']['total'], 2)
        restarted = KiCadProvider(self.settings, self.execute_cli)
        self.assertEqual(restarted._load(result['work_copy_id'])[1]['source'], str(self.project))

    def test_paths_credentials_symlinks_and_ambiguous_boards(self):
        self.failure('path_denied', 'check_kicad_board', project_dir=str(self.root))
        for name in ('.env', 'nested/credentials.json', 'cert.key', '.alx/state'):
            path = self.project / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('private')
            self.failure('path_denied', 'check_kicad_board', project_dir=str(self.project))
            path.unlink()
            if path.parent != self.project:
                path.parent.rmdir()
        link = self.project / 'escape'
        link.symlink_to(self.root, target_is_directory=True)
        self.failure('path_denied', 'check_kicad_board', project_dir=str(self.project))
        link.unlink()
        (self.project / 'second.kicad_pcb').write_text(BOARD)
        self.failure('unsupported_project', 'check_kicad_board', project_dir=str(self.project))
        self.assertEqual(self.calls, [])


    def test_cli_failure_diagnostics_never_include_board_content(self):
        private = '(gr_text "private board text")'
        for outcome in (SimpleNamespace(returncode=2, stderr=private),
                        subprocess.TimeoutExpired('kicad-cli', 600, stderr=private)):
            execute = Mock(**({'side_effect': outcome} if isinstance(outcome, Exception)
                              else {'return_value': outcome}))
            provider = KiCadProvider(self.settings, execute)
            with self.assertLogs('alx.providers.kicad', level='WARNING') as logs:
                with self.assertRaises(KiCadError):
                    provider._run('pcb', 'export', 'svg')
            self.assertNotIn(private, str(logs.output))
            self.assertTrue(all(record.exc_info is None for record in logs.records))


    def fixed(self):
        checked = self.check()
        return self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                            script=EDIT_SCRIPT)

    def write_args(self, result):
        return {'work_copy_id': result['work_copy_id'], 'work_copy_revision': result['work_copy_revision'],
                'project_dir': str(self.project)}

    def test_write_locks_original_inventory_and_revision_refusals(self):
        result = self.fixed()
        args = self.write_args(result)
        lock = self.project / '~test.kicad_pcb.lck'
        lock.write_text('open')
        self.failure('board_open', 'write_back_kicad_board', **args)
        lock.unlink()
        (self.project / 'new.txt').write_text('changed inventory')
        self.failure('original_changed', 'write_back_kicad_board', **args)
        (self.project / 'new.txt').unlink()
        (self.project / 'test.kicad_pcb').write_text(BOARD + '\n')
        self.failure('original_changed', 'write_back_kicad_board', **args)
        (self.project / 'test.kicad_pcb').write_text(BOARD)
        args['work_copy_revision'] = 'stale'
        self.failure('work_copy_changed', 'write_back_kicad_board', **args)

    def test_exact_broker_approval_backup_before_write_and_read_back(self):
        fixed = self.fixed()
        args = self.write_args(fixed)
        broker = CapabilityBroker(CapabilityRegistry(DEFINITIONS), SafetyGate(self.runtime.policies), self.runtime.executors)
        now = datetime.now(UTC)
        context = AuthorityContext('friedl', self.runtime.permissions, now)
        call = CapabilityCall('call', 'write_back_kicad_board', args)
        self.assertEqual(broker.dispatch(call, context).state.value, 'rejected')
        self.assertEqual((self.project / 'test.kicad_pcb').read_text(), BOARD)
        approval = Approval('approved', ApprovalScope(call.capability_id, args),
                            ApprovalLifecycle.GRANTED, now + timedelta(minutes=1))
        context = AuthorityContext('friedl', self.runtime.permissions, now, (approval,))
        changed = dict(args, work_copy_revision='different')
        self.assertEqual(broker.dispatch(CapabilityCall('call', call.capability_id, changed, 'approved'), context).state.value, 'rejected')
        import shutil
        copy = shutil.copy2
        events = []
        def copying(source, target):
            events.append(str(target))
            if Path(target).is_relative_to(self.project):
                backups = list(self.settings.work_root.rglob('backups/*/test.kicad_pcb'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), BOARD)
            return copy(source, target)
        with patch('alx.providers.kicad.shutil.copy2', side_effect=copying):
            outcome = broker.dispatch(CapabilityCall('call', call.capability_id, args, 'approved'), context)
        self.assertEqual(outcome.result.state, CapabilityResultState.SUCCEEDED, outcome.result.failure)
        self.assertTrue(outcome.result.values['written'])
        self.assertIn('backups', events[0])
        self.assertIn('(size 1 1)', (self.project / 'test.kicad_pcb').read_text())
        self.assertFalse(any(p.suffix in ('.json', '.svg') for p in self.project.iterdir()))

    def test_invalid_work_ids_cli_and_reports(self):
        self.failure('path_denied', 'edit_kicad_work_copy', work_copy_id='../outside', script='pass')
        provider = KiCadProvider(self.settings, lambda *a, **k: SimpleNamespace(returncode=1))
        with self.assertRaisesRegex(KiCadError, 'cli_failed'):
            provider.check({'project_dir': str(self.project)})

        def malformed(executable, arguments):
            Path(arguments[arguments.index('--output') + 1]).write_text('{malformed')
            return SimpleNamespace(returncode=0)
        provider = KiCadProvider(self.settings, malformed)
        with self.assertRaisesRegex(KiCadError, 'invalid_report'):
            provider.check({'project_dir': str(self.project)})

    def test_provider_forwards_executable_and_string_arguments(self):
        execute_cli = Mock(return_value=SimpleNamespace(returncode=0))
        provider = KiCadProvider(self.settings, execute_cli)
        board = self.project / 'board with spaces.kicad_pcb'
        provider._run('pcb', 'drc', '--output', board)
        execute_cli.assert_called_once_with(
            str(self.settings.kicad_cli), ['pcb', 'drc', '--output', str(board)])

    def test_cli_uses_approved_site_with_fixed_argv_and_options(self):
        arguments = ['pcb', 'drc', 'board with spaces;$(echo test).kicad_pcb']
        for runner in (None, Mock()):
            with self.subTest(default_runner=runner is None), \
                    patch('alx.providers.coding_process.run_coding_subprocess') as execute:
                result = run_kicad_cli('/path with spaces/kicad-cli', arguments, runner=runner)
                execute.assert_called_once_with(
                    subprocess.run if runner is None else runner,
                    ['/path with spaces/kicad-cli', *arguments],
                    shell=False, check=False, timeout=600, capture_output=True, text=True,
                    stop_descendants=True)
                self.assertIs(result, execute.return_value)

    def test_cli_os_error_and_timeout_are_distinct(self):
        for error, code in ((OSError('missing CLI'), 'unavailable'),
                            (subprocess.TimeoutExpired('kicad-cli', 600, stderr=b'export stalled'), 'cli_timeout')):
            with self.subTest(error=type(error).__name__):
                runner = Mock(side_effect=error)
                provider = KiCadProvider(self.settings,
                    lambda executable, arguments: run_kicad_cli(executable, arguments, runner=runner))
                with self.assertLogs('alx.providers.kicad', level='WARNING') as logs:
                    with self.assertRaisesRegex(KiCadError, code) as caught:
                        provider._run('pcb', 'export', 'svg')
                self.assertIn('pcb export svg', logs.output[0])
                if code == 'cli_timeout':
                    self.assertIn('timed out', logs.output[0])
                    self.assertNotIn('export stalled', logs.output[0])
                self.assertIs(caught.exception.__cause__, error)
                runtime = build_kicad_runtime(lambda: 'call', self.environment, self.storage, runner)
                result = runtime.executors['check_kicad_board']({'project_dir': str(self.project)})
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure['code'], code)

    def test_timeout_configuration_and_failed_cli_diagnostics(self):
        self.assertEqual(self.settings.cli_timeout_seconds, 600)
        environment = dict(self.environment, ALX_KICAD_CLI_TIMEOUT_SECONDS='875')
        runner = Mock(return_value=SimpleNamespace(returncode=2, stderr='x' * 3000 + '\npassword=private-value\x1b'))
        runtime = build_kicad_runtime(lambda: 'call', environment, self.storage, runner)
        with self.assertLogs('alx.providers.kicad', level='WARNING') as logs:
            result = runtime.executors['check_kicad_board']({'project_dir': str(self.project)})
        self.assertEqual(result.failure['code'], 'cli_failed')
        self.assertEqual(runner.call_args.kwargs['timeout'], 875)
        self.assertIn('pcb drc failed (2)', logs.output[0])
        self.assertNotIn('private-value', logs.output[0])
        self.assertNotIn('\x1b', logs.output[0])
        self.assertLess(len(logs.output[0]), 2200)
        for invalid in ('0', '-1', 'nan', '1.2'):
            with self.assertRaises(ConfigurationError):
                KiCadSettings.from_environment(dict(environment, ALX_KICAD_CLI_TIMEOUT_SECONDS=invalid), self.storage)


    def test_rule_citations_require_loaded_name_closing_boundary(self):
        (self.project / 'test.kicad_dru').write_text(
            '(version 1)\n(rule "U1" (constraint clearance (min 0.1mm)))')
        self.report = {'violations': [{'type': 'clearance', 'severity': 'error', 'items': [],
            'description': "rule 'U1's pads'; rule 'U1 extended'; rule 'U1'"}],
            'unconnected_items': [], 'schematic_parity': []}
        self.assertEqual([dict(c) for c in self.check()['custom_rule_citations']], [
            {'name': 'U1', 'count': 1}, {'name': 'U1 extended', 'count': 1},
            {'name': "U1's pads", 'count': 1}])

    def test_rule_citations_prefer_loaded_names_and_keep_multiple_citations_separate(self):
        names = ['U1', "U1'; pads", "U1'; tracks", "Nested rule 'pads'"]
        (self.project / 'test.kicad_dru').write_text('(version 1)\n' + '\n'.join(
            f'(rule {json.dumps(name)} (constraint clearance (min 0.1mm)))'
            for name in names))
        finding = {'type': 'clearance', 'severity': 'error', 'items': []}
        # Loaded names take precedence over unrelated quoted diagnostic text.
        self.report = {'violations': [dict(finding,
            description="rule 'U1'; diagnostic 'unrelated'")],
            'unconnected_items': [dict(finding, description='; '.join(
                f"rule '{name}'" for name in names))],
            'schematic_parity': [dict(finding, description="rule 'U1'; pads'")]}
        self.assertEqual([dict(c) for c in self.check()['custom_rule_citations']], [
            {'name': "Nested rule 'pads'", 'count': 1}, {'name': 'U1', 'count': 2},
            {'name': "U1'; pads", 'count': 2}, {'name': "U1'; tracks", 'count': 1}])

    def test_custom_noise_configuration_is_the_only_flag_source(self):
        environment = dict(self.environment, ALX_KICAD_NOISE_CHECKS='unconnected_items')
        runtime = build_kicad_runtime(lambda: 'call', environment, self.storage, self.run_cli)
        result = runtime.executors['check_kicad_board']({'project_dir': str(self.project)})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(result.values['violations'][0]['noise'])
        self.assertTrue(result.values['unconnected_items'][0]['noise'])


    def test_late_original_change_and_corrupted_write_are_detected(self):
        fixed = self.fixed()
        args = self.write_args(fixed)
        import shutil
        copy = shutil.copy2
        def concurrent(source, target):
            result = copy(source, target)
            if 'backups' in Path(target).parts:
                (self.project / 'test.kicad_pcb').write_text(BOARD + '\n')
            return result
        with patch('alx.providers.kicad.shutil.copy2', side_effect=concurrent):
            self.failure('verification_failed', 'write_back_kicad_board', **args)
        (self.project / 'test.kicad_pcb').write_text(BOARD)
        def corrupt(source, target):
            result = copy(source, target)
            if Path(target).is_relative_to(self.project):
                Path(target).write_text('corrupt')
            return result
        with patch('alx.providers.kicad.shutil.copy2', side_effect=corrupt):
            self.failure('verification_failed', 'write_back_kicad_board', **args)
        backups = list(self.settings.work_root.rglob('backups/*/test.kicad_pcb'))
        self.assertTrue(backups)
        self.assertTrue(all(p.read_text() == BOARD for p in backups))

    def two_file_write(self):
        checked = self.check()
        edited = self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                              script=EDIT_SCRIPT + TWO_FILE_SCRIPT)
        return self.write_args(edited)

    def test_second_copy_failure_cannot_leave_mixed_originals(self):
        import shutil
        args = self.two_file_write()
        original = inventory(self.project)
        copy = shutil.copy2
        count = 0
        def copying(source, target):
            nonlocal count
            if Path(target).parent == self.project:
                count += 1
                if count == 2:
                    raise OSError('second file failed')
            return copy(source, target)
        with patch('alx.providers.kicad.shutil.copy2', side_effect=copying):
            result = self.call('write_back_kicad_board', **args)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(inventory(self.project), original)
        self.assertIn('recovery', result.failure)

    def test_partial_failure_preserves_files_and_recovery_evidence(self):
        import os
        for fault in ('replace', 'lock', 'readback'):
            with self.subTest(fault=fault):
                args = self.two_file_write()
                replace = os.replace
                count = 0
                def replacing(source, target):
                    nonlocal count
                    if '.stage-' in Path(source).name:
                        count += 1
                        if count == 2 and fault == 'replace':
                            raise OSError('second replace failed')
                        result = replace(source, target)
                        if count == 1 and fault == 'lock':
                            (self.project / '~test.lck').write_text('open')
                        if count == 1 and fault == 'readback':
                            Path(target).write_text('corrupt')
                        return result
                    return replace(source, target)
                with patch('alx.providers.kicad.os.replace', side_effect=replacing):
                    result = self.call('write_back_kicad_board', **args)
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                recovery = result.failure['recovery']
                self.assertEqual(len(recovery['written']), 1)
                self.assertEqual(len(recovery['backed_up']), 2)
                self.assertEqual(recovery['rollback_status'], 'failed')
                self.assertEqual(recovery['remaining_changed'], recovery['written'])
                self.assertFalse(recovery['restorations'][0]['restored'])
                reason = ('write_digest_mismatch' if fault == 'readback'
                          else 'atomic_recovery_unavailable')
                self.assertEqual(recovery['conflicts'][0]['reason'], reason)
                (self.project / '~test.lck').unlink(missing_ok=True)
                for backup in recovery['backed_up']:
                    target = self.project / Path(backup).name
                    self.assertTrue(Path(backup).is_file())
                    target.write_bytes(Path(backup).read_bytes())

    def test_staging_failure_never_changes_originals(self):
        import shutil
        for fault in ('copy', 'readback'):
            args = self.two_file_write()
            original = inventory(self.project)
            copy = shutil.copy2
            def copying(source, target):
                if '.stage-' in Path(target).name:
                    if fault == 'copy':
                        raise OSError('staging failed')
                    copy(source, target)
                    Path(target).write_text('corrupt')
                    return target
                return copy(source, target)
            with patch('alx.providers.kicad.shutil.copy2', side_effect=copying):
                result = self.call('write_back_kicad_board', **args)
            self.assertEqual(result.state, CapabilityResultState.FAILED)
            self.assertFalse(result.failure['recovery']['written'])
            self.assertEqual(inventory(self.project), original)

    def test_original_hash_is_rechecked_between_replacements(self):
        import os
        args = self.two_file_write()
        replace = os.replace
        changed = []
        def replacing(source, target):
            result = replace(source, target)
            if '.stage-' in Path(source).name:
                changed.append(str(target))
                other = next(p for p in self.project.glob('test.*') if p != Path(target))
                other.write_text('concurrent edit')
            return result
        with patch('alx.providers.kicad.os.replace', side_effect=replacing):
            result = self.call('write_back_kicad_board', **args)
        self.assertEqual(result.failure['code'], 'original_changed')
        self.assertEqual(len(changed), 1)
        self.assertIn('(size 1 1)', Path(changed[0]).read_text())
        self.assertIn(b'concurrent edit', [p.read_bytes() for p in self.project.iterdir()])
        recovery = result.failure['recovery']
        self.assertEqual(recovery['rollback_status'], 'failed')
        self.assertEqual(len(recovery['remaining_changed']), 2)
        self.assertIn(changed[0], recovery['remaining_changed'])

    def test_recovery_preserves_new_custom_rules_without_atomic_delete(self):
        import os
        args = self.two_file_write()
        provider = KiCadProvider(self.settings, self.execute_cli)
        root = self.settings.work_root / args['work_copy_id']
        (root / 'project' / 'test.kicad_dru').write_text('(version 1)')
        args['work_copy_revision'] = provider._revision(root)
        replace = os.replace
        def replacing(source, target):
            result = replace(source, target)
            if Path(target).suffix == '.kicad_dru':
                (self.project / '~test.lck').write_text('open')
            return result
        with patch('alx.providers.kicad.os.replace', side_effect=replacing):
            result = self.call('write_back_kicad_board', **args)
        self.assertEqual(result.failure['code'], 'board_open')
        recovery = result.failure['recovery']
        self.assertEqual(len(recovery['written']), 3)
        self.assertEqual(len(recovery['backed_up']), 2)
        self.assertEqual(recovery['rollback_status'], 'failed')
        self.assertCountEqual(recovery['remaining_changed'], recovery['written'])
        self.assertTrue((self.project / 'test.kicad_dru').is_file())
        (self.project / '~test.lck').unlink()

    def test_provider_reports_every_deferred_restoration(self):
        import os
        args = self.two_file_write()
        provider = KiCadProvider(self.settings, self.execute_cli)
        root = self.settings.work_root / args['work_copy_id']
        (root / 'project' / 'test.kicad_dru').write_text('(version 1)')
        args['work_copy_revision'] = provider._revision(root)
        replace = os.replace
        replaced = []
        def replacing(source, target):
            if '.stage-' in Path(source).name:
                if len(replaced) == 2:
                    raise OSError('third file failed')
                replaced.append(str(target))
            return replace(source, target)
        with patch('alx.providers.kicad.os.replace', side_effect=replacing):
            with self.assertRaises(KiCadError) as caught:
                provider.write_back(args)
        recovery = caught.exception.recovery
        self.assertEqual(recovery['written'], replaced)
        self.assertEqual(len(recovery['restorations']), 2)
        self.assertEqual([r['restored'] for r in recovery['restorations']], [False, False])
        self.assertCountEqual(recovery['remaining_changed'], replaced)
        self.assertEqual(recovery['rollback_status'], 'failed')
        self.assertEqual(recovery['error']['type'], 'OSError')

    def test_recovery_preserves_external_edits_to_replaced_and_new_files(self):
        import os
        for name in ('test.kicad_pcb', 'test.kicad_dru'):
            with self.subTest(name=name):
                args = self.two_file_write()
                provider = KiCadProvider(self.settings, self.execute_cli)
                root = self.settings.work_root / args['work_copy_id']
                (root / 'project' / 'test.kicad_dru').write_text('(version 1)')
                args['work_copy_revision'] = provider._revision(root)
                replace = os.replace
                def replacing(source, target):
                    result = replace(source, target)
                    if '.stage-' in Path(source).name and Path(target).name == name:
                        Path(target).write_text('external edit')
                    return result
                with patch('alx.providers.kicad.os.replace', side_effect=replacing):
                    with self.assertRaises(KiCadError) as caught:
                        provider.write_back(args)
                recovery = caught.exception.recovery
                self.assertEqual((self.project / name).read_text(), 'external edit')
                self.assertIn(str(self.project / name), recovery['remaining_changed'])
                conflict = next(c for c in recovery['conflicts']
                                if c['original'] == str(self.project / name))
                self.assertEqual(conflict['reason'], 'write_digest_mismatch')
                self.assertNotEqual(conflict['expected_digest'], conflict['current_digest'])
                self.assertEqual(recovery['rollback_status'], 'failed')
                (self.project / 'test.kicad_dru').unlink(missing_ok=True)
                (self.project / 'test.kicad_pcb').write_text(BOARD)
                (self.project / 'test.kicad_pro').write_text(json.dumps(PROJECT))

    def test_transaction_temporaries_do_not_consume_inventory_limits(self):
        for limit in ('MAX_PROJECT_ENTRIES', 'MAX_PROJECT_BYTES'):
            with self.subTest(limit=limit):
                args = self.two_file_write()
                provider = KiCadProvider(self.settings, self.execute_cli)
                real = inventory(self.project)
                budget = (len(real) if limit.endswith('ENTRIES') else
                          sum(p.stat().st_size for p in (self.settings.work_root /
                              args['work_copy_id'] / 'project').iterdir()) + 100)
                real_inventory = inventory
                def limited(root, **kwargs):
                    if root == self.project:
                        with patch('alx.providers.kicad.' + limit, budget):
                            return real_inventory(root, **kwargs)
                    return real_inventory(root, **kwargs)
                with patch('alx.providers.kicad.inventory', side_effect=limited):
                    provider.write_back(args)
                # Similar names belonging to users must still count.
                user = self.project / '.test.kicad_pcb.stage-user'
                user.write_bytes(b'x' * (budget + 1))
                with patch('alx.providers.kicad.' + limit, budget):
                    with self.assertRaisesRegex(KiCadError, 'project_too_large'):
                        inventory(self.project)
                user.unlink()
                (self.project / 'test.kicad_pcb').write_text(BOARD)
                (self.project / 'test.kicad_pro').write_text(json.dumps(PROJECT))

    def test_recovery_does_not_mutate_after_digest_observation(self):
        import os
        from alx.providers.kicad import sha
        args = self.two_file_write()
        replace = os.replace
        written = []
        recovering = False
        def replacing(source, target):
            nonlocal recovering
            if '.stage-' in Path(source).name:
                if written:
                    recovering = True
                    raise OSError('second replacement failed')
                written.append(Path(target))
            return replace(source, target)
        def reading(path):
            digest = sha(path)
            if recovering and path in written:
                path.write_text('edit after digest observation')
            return digest
        with patch('alx.providers.kicad.os.replace', side_effect=replacing), \
                patch('alx.providers.kicad.sha', side_effect=reading):
            result = self.call('write_back_kicad_board', **args)
        self.assertEqual(written[0].read_text(), 'edit after digest observation')
        recovery = result.failure['recovery']
        self.assertEqual(recovery['rollback_status'], 'failed')
        self.assertFalse(any(r['restored'] for r in recovery['restorations']))

    def test_exact_temp_exclusions_still_validate_real_files(self):
        baseline = inventory(self.project)
        stage = self.project / '.test.kicad_pcb.stage-owned'
        backup = self.project / '.test.kicad_pcb.backup-owned'
        for path in (stage, backup):
            path.write_bytes(b'x' * 1000)
        excluded = {stage, backup}
        budget = sum((self.project / name).stat().st_size for name in baseline)
        with patch('alx.providers.kicad.MAX_PROJECT_BYTES', budget), \
                patch('alx.providers.kicad.MAX_PROJECT_ENTRIES', len(baseline)):
            self.assertEqual(inventory(self.project, exclude=excluded), baseline)
        real = self.project / 'test.kicad_pro'
        real.unlink()
        real.symlink_to(stage)
        with self.assertRaisesRegex(KiCadError, 'path_denied'):
            inventory(self.project, exclude=excluded)

    def test_recovery_keeps_unverifiable_paths(self):
        import os
        read_bytes = Path.read_bytes
        for kind in ('missing', 'symlink', 'directory', 'unreadable'):
            with self.subTest(kind=kind):
                replace = os.replace
                replaced = False
                target = self.project / 'test.kicad_pcb'
                other = self.project / 'external.txt'
                other.write_text('external contents')
                def replacing(source, destination):
                    nonlocal replaced
                    result = replace(source, destination)
                    if Path(destination) == target and '.stage-' in Path(source).name:
                        replaced = True
                        if kind != 'unreadable':
                            target.unlink()
                            if kind == 'symlink':
                                target.symlink_to(other)
                            elif kind == 'directory':
                                target.mkdir()
                    return result
                def reading(path):
                    if kind == 'unreadable' and replaced and path == target:
                        raise PermissionError('simulated unreadable replaced board')
                    return read_bytes(path)
                # Include the user file in the approved snapshot.
                args = self.two_file_write()
                with patch('alx.providers.kicad.os.replace', side_effect=replacing), \
                        patch.object(Path, 'read_bytes', reading):
                    result = self.call('write_back_kicad_board', **args)
                recovery = result.failure['recovery']
                self.assertEqual(recovery['rollback_status'], 'failed')
                self.assertEqual(recovery['conflicts'][0]['reason'],
                                 'current_contents_unverifiable')
                self.assertEqual(other.read_text(), 'external contents')
                if kind == 'directory':
                    target.rmdir()
                else:
                    target.unlink(missing_ok=True)
                target.write_text(BOARD)
                other.unlink()

    def test_settings_defaults_and_storage_bound(self):
        defaults = KiCadSettings.from_environment({}, self.storage)
        self.assertEqual(defaults.kicad_cli.name, 'kicad-cli')
        self.assertEqual(defaults.design_roots[0], Path('~/Desktop').expanduser().resolve())
        self.assertEqual(defaults.noise_checks, ('lib_footprint_mismatch', 'malformed_courtyard'))
        with self.assertRaises(ConfigurationError):
            KiCadSettings.from_environment({'ALX_KICAD_WORK_ROOT': str(self.design)}, self.storage)

    def test_removed_mechanical_path_is_absent_from_production(self):
        import ast
        source = Path(__file__).parents[1] / 'src/alx'
        for path in source.rglob('*.py'):
            self.assertNotIn('apply_kicad_mechanical_fixes', path.read_text(), str(path))
        provider = ast.parse((source / 'providers/kicad.py').read_text())
        cls = next(n for n in provider.body if isinstance(n, ast.ClassDef) and n.name == 'KiCadProvider')
        self.assertNotIn('fix', [n.name for n in cls.body if isinstance(n, ast.FunctionDef)])

    def test_edit_reports_graphic_layer_move_and_new_revision_then_writes_back(self):
        checked = self.check()
        edited = self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script=EDIT_SCRIPT)
        self.assertNotEqual(checked['work_copy_revision'], edited['work_copy_revision'])
        report = edited['change_report']
        self.assertEqual(report['files_changed'], ('test.kicad_pcb',))
        move = next(m for m in report['board']['layer_moves'] if m['id'] == 'graphic-id')
        self.assertEqual(move['before'], ('B.Fab',))
        self.assertEqual(move['after'], ('User.1',))
        self.assertEqual(report['board']['per_layer_after']['User.1'], 1)
        self.assertEqual(edited['before']['total'], edited['after']['total'])
        self.assertEqual((self.project / 'test.kicad_pcb').read_text(), BOARD)
        self.success('write_back_kicad_board', **self.write_args(edited))
        self.assertIn('(layer "User.1")', (self.project / 'test.kicad_pcb').read_text())

    def test_failed_edit_restores_entire_copy_and_absent_files(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        original = inventory(root / 'project')
        script = EDIT_SCRIPT + "\np.with_suffix('.kicad_dru').write_text('(version 1)')\nraise RuntimeError('failed')"
        result = self.call('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script=script)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['recovery']['rollback_status'], 'restored')
        self.assertEqual(inventory(root / 'project'), original)
        restarted = KiCadProvider(self.settings, self.execute_cli)
        self.assertEqual(restarted._revision(root), checked['work_copy_revision'])

    def test_failed_restore_retains_durable_taint_and_blocks_write_and_edit(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        import shutil
        rmtree = shutil.rmtree
        def remove(path, *args, **kwargs):
            if Path(path) == root / 'project':
                raise OSError('restore failed')
            return rmtree(path, *args, **kwargs)
        with patch('alx.providers.kicad.shutil.rmtree', side_effect=remove):
            result = self.call('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                               script=EDIT_SCRIPT + "\nraise RuntimeError('failed')")
        self.assertTrue(result.failure['recovery']['work_copy_tainted'])
        self.assertTrue(json.loads((root / 'metadata.json').read_text())['taint'])
        self.failure('work_copy_tainted', 'edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script='pass')
        provider = KiCadProvider(self.settings, self.execute_cli)
        self.failure('work_copy_tainted', 'write_back_kicad_board', **dict(
            self.write_args(checked), work_copy_revision=provider._revision(root)))

    def test_python_execution_is_confined_and_bounded_at_approved_site(self):
        from alx.providers.coding_process import run_kicad_python
        script = self.root / 'edit.py'
        script.write_text('pass')
        result = SimpleNamespace(returncode=0, stdout='x' * 20000, stderr='')
        with patch('alx.providers.coding_process.run_coding_subprocess', return_value=result) as run:
            outcome = run_kicad_python('python', script, self.project / 'test.kicad_pcb', timeout_seconds=999)
        self.assertEqual(len(outcome.stdout), 16000)
        argv = run.call_args.args[1]
        self.assertEqual(argv[:2], ['/usr/bin/sandbox-exec', '-p'])
        self.assertIn('(allow default)', argv[2])
        self.assertIn('(deny file-write*)', argv[2])
        self.assertIn('(deny network*)', argv[2])
        self.assertIn('(allow file-write* (subpath ' + json.dumps(str(self.project))
                      + ') (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))', argv[2])
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        self.assertIn('(deny file-read* (subpath ' + json.dumps(str(home)) + '))', argv[2])
        self.assertIn('(allow file-read* (subpath ' + json.dumps(str(self.project)) + ') (literal '
                      + json.dumps(str(script.resolve())) + '))', argv[2])
        self.assertIn('(deny process-fork)', argv[2])
        self.assertEqual(run.call_args.kwargs['env']['HOME'], str(self.project))
        self.assertEqual(run.call_args.kwargs['env']['TMPDIR'], str(self.project))
        self.assertEqual(argv[-1], str(self.project / 'test.kicad_pcb'))
        self.assertEqual(run.call_args.kwargs['timeout'], 600)
        self.assertTrue(run.call_args.kwargs['stop_descendants'])
        with patch('alx.providers.coding_process.kicad_containment_available', REAL_CONTAINMENT), \
                patch('alx.providers.coding_process.platform.system', return_value='Linux'):
            with self.assertRaises(OSError):
                run_kicad_python('python', script, self.project / 'test.kicad_pcb')

    def manufacturing_runtime(self):
        def runner(argv, **kwargs):
            if argv[2] == 'drc':
                return self.run_cli(argv, **kwargs)
            target = Path(argv[argv.index('--output') + 1])
            if argv[3] == 'gerbers':
                (target / 'test-User_1.gbr').write_text('G04 Created today*\n%TF.CreationDate,new*%\n%TF.GenerationSoftware,new*%\nX123Y456D01*\n')
                (target / 'test-F_Cu.gbr').write_text('X987Y654D01*\n')
            else:
                (target / 'test.drl').write_text('M48\nT1C0.25\n')
            return SimpleNamespace(returncode=0)
        return build_kicad_runtime(lambda: 'export', self.environment, self.storage, runner)

    def test_manufacturing_renames_zip_and_add_only_preflight(self):
        import zipfile
        runtime = self.manufacturing_runtime()
        checked = self.check()
        args = {'work_copy_id': checked['work_copy_id'], 'output_dir': 'production/test',
                'renames': {'User.1': 'pib_0.15.gbr', 'test-F_Cu.gbr': 'top.gbr'}, 'zip_name': 'maker.zip'}
        result = runtime.executors['export_kicad_manufacturing'](args)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        dest = self.project / 'production/test'
        self.assertEqual(set(p.name for p in dest.iterdir()), {'pib_0.15.gbr', 'top.gbr', 'test.drl', 'maker.zip'})
        with zipfile.ZipFile(dest / 'maker.zip') as bundle:
            self.assertEqual(set(bundle.namelist()), {'pib_0.15.gbr', 'top.gbr', 'test.drl'})
        before = inventory(dest)
        (dest / 'top.gbr').unlink()
        result = runtime.executors['export_kicad_manufacturing'](args)
        self.assertEqual(result.failure['code'], 'export_would_overwrite')
        self.assertFalse((dest / 'top.gbr').exists())
        self.assertEqual(inventory(dest), {k: v for k, v in before.items() if k != 'top.gbr'})

    def test_manufacturing_saved_plot_settings_and_separate_drills(self):
        checked = self.check()
        for option in (None, False, True):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                target = Path(argv[argv.index('--output') + 1])
                if argv[3] == 'gerbers':
                    (target / 'test-F_Cu.gtl').write_text('%TF.FileFunction,Copper,L1,Top*%\nX1Y2D01*\n')
                else:
                    names = ('test-PTH.drl', 'test-NPTH.drl') if '--excellon-separate-th' in argv else ('test.drl',)
                    for name in names:
                        (target / name).write_text('M48\nT1C0.25\n')
                return SimpleNamespace(returncode=0)
            runtime = build_kicad_runtime(lambda: 'export', self.environment, self.storage, runner)
            args = {'work_copy_id': checked['work_copy_id'], 'output_dir': 'production/' + str(option),
                    'renames': {'F.Cu': 'top.gbr'}}
            if option is not None:
                args['separate_drills'] = option
            result = runtime.executors['export_kicad_manufacturing'](args)
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            gerbers, drill = calls
            self.assertIn('--board-plot-params', gerbers)
            self.assertNotIn('--layers', gerbers)
            self.assertNotIn('--no-protel-ext', gerbers)
            self.assertEqual('--excellon-separate-th' in drill, option is True)
            expected = {'top.gbr', 'test-PTH.drl', 'test-NPTH.drl'} if option else {'top.gbr', 'test.drl'}
            self.assertEqual({Path(p).name for p in result.values['files_written']}, expected)
        result = runtime.executors['export_kicad_manufacturing'](dict(args, separate_drills='yes'))
        self.assertEqual(result.failure['code'], 'arguments_unusable')

    def test_manufacturing_comparison_ignores_dates_comments_and_generator(self):
        from alx.providers.kicad import compare_manufacturing
        import zipfile
        old = self.root / 'previous'
        old.mkdir()
        (old / 'top.gbr').write_text('G04 old comment*\n%TF.CreationDate,yesterday*%\n%TF.GenerationSoftware,old*%\nX123Y456D01*\n')
        (old / 'gone.gbr').write_text('gone')
        new = {'top.gbr': b'G04 anything*\n%TF.CreationDate,today*%\nX123Y456D01*\n', 'new.gbr': b'new'}
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            first = compare_manufacturing(new, old)
            archive = self.root / 'previous.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                for p in old.iterdir():
                    bundle.write(p, p.name)
            self.assertEqual(compare_manufacturing(new, archive), first)
            self.assertEqual({f['name']: f['status'] for f in first['files']},
                             {'top.gbr': 'identical', 'gone.gbr': 'only_in_previous', 'new.gbr': 'only_in_new'})
            new['top.gbr'] += b'X9Y9D01*\n'
            changed = compare_manufacturing(new, old)
            self.assertEqual(changed['files'][-1]['status'], 'differs')
            self.assertEqual(changed['files'][-1]['differing_lines'], 1)

    def test_export_rejects_path_escape_renaming_collision_and_symlink(self):
        runtime = self.manufacturing_runtime()
        checked = self.check()
        for output, renames in (('../escape', {}), ('/absolute', {}), ('production', {'User.1': '../escape'}),
                                ('production', {'User.1': 'test-F_Cu.gbr'})):
            result = runtime.executors['export_kicad_manufacturing']({
                'work_copy_id': checked['work_copy_id'], 'output_dir': output, 'renames': renames})
            self.assertEqual(result.state, CapabilityResultState.FAILED)
        (self.project / 'linked').symlink_to(self.root)
        result = runtime.executors['export_kicad_manufacturing']({
            'work_copy_id': checked['work_copy_id'], 'output_dir': 'linked/production'})
        self.assertEqual(result.failure['code'], 'path_denied')

    def test_references_text_zip_folder_pdf_image_altium_and_icloud(self):
        import zipfile
        import struct
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            reference = self.root / 'Library/Mobile Documents/com~apple~CloudDocs/references'
            reference.mkdir(parents=True)
            text = reference / 'notes.txt'
            text.write_text('coverlay 0.05 mm')
            self.assertEqual(self.success('read_reference_file', path=str(text))['text'], 'coverlay 0.05 mm')
            self.assertEqual(self.success('read_reference_file', path=str(reference))['kind'], 'folder')
            archive = reference / 'previous.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('top.gbr', 'X1Y2D01*')
            self.assertEqual(self.success('read_reference_file', path=str(archive))['entries'][0]['name'], 'top.gbr')
            self.assertEqual(self.success('read_reference_file', path=str(archive), entry='top.gbr')['text'], 'X1Y2D01*')
            pdf = reference / 'report.pdf'
            writer = PdfWriter()
            page = writer.add_blank_page(100, 100)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf (Coverlay) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
            with pdf.open('wb') as file:
                writer.write(file)
            self.assertIn('Coverlay', self.success('read_reference_file', path=str(pdf))['pages'][0]['text'])
            image = reference / 'board.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 8 + struct.pack('>II', 320, 200))
            self.assertEqual(self.success('read_reference_file', path=str(image))['width'], 320)
            project = reference / 'board.PrjPcb'
            project.write_text('[Document1]\nDocumentPath=board.PcbDoc\n')
            self.assertEqual(self.success('read_reference_file', path=str(project))['documents'], ('board.PcbDoc',))
            (reference / '.board.PcbDoc.icloud').write_text('placeholder')
            self.assertEqual(self.success('read_reference_file', path=str(reference / 'board.PcbDoc'))['kind'], 'not_downloaded')
            self.assertEqual(self.success('read_reference_file', path=str(reference / '.board.PcbDoc.icloud'))['kind'], 'not_downloaded')

    def test_reference_credential_refusal_and_symlink_escape(self):
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            for name in ('.ssh/id_rsa', '.env', 'Library/Keychains/login.keychain-db', 'tokens.json', '.env.local'):
                self.failure('path_denied', 'read_reference_file', path=str(self.root / name))
            self.failure('path_denied', 'read_reference_file', path='/etc/passwd')
            link = self.root / 'link'
            link.symlink_to('/etc/passwd')
            self.failure('path_denied', 'read_reference_file', path=str(link))

    def test_altium_ole_returns_hierarchical_stream_names(self):
        import struct
        header = bytearray(512)
        header[:8] = bytes.fromhex('d0cf11e0a1b11ae1')
        struct.pack_into('<H', header, 30, 9)
        struct.pack_into('<I', header, 48, 1)
        struct.pack_into('<II', header, 68, 0xfffffffe, 0)
        struct.pack_into('<109I', header, 76, 0, *([0xffffffff] * 108))
        fat = bytearray(b'\xff' * 512)
        struct.pack_into('<II', fat, 0, 0xfffffffd, 0xfffffffe)
        directory = bytearray(512)
        for index, (name, kind, child) in enumerate((('Root Entry', 5, 1), ('Board6', 1, 2), ('Data', 2, 0xffffffff))):
            offset = index * 128
            encoded = (name + '\0').encode('utf-16-le')
            directory[offset:offset + len(encoded)] = encoded
            struct.pack_into('<H', directory, offset + 64, len(encoded))
            directory[offset + 66] = kind
            struct.pack_into('<III', directory, offset + 68, 0xffffffff, 0xffffffff, child)
        board = self.root / 'board.PcbDoc'
        board.write_bytes(header + fat + directory)
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            result = self.success('read_reference_file', path=str(board))
        self.assertEqual(result['streams'], ('Board6/Data',))
        self.assertFalse(result['board_records_decoded'])

    def test_own_exports_do_not_block_later_guarded_writeback(self):
        edited = self.fixed()
        runtime = self.manufacturing_runtime()
        result = runtime.executors['export_kicad_manufacturing']({
            'work_copy_id': edited['work_copy_id'], 'output_dir': 'production'})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.success('write_back_kicad_board', **self.write_args(edited))
        self.assertIn('(layer "User.1")', (self.project / 'test.kicad_pcb').read_text())

    LONG_EDIT = ("import sys\nfrom pathlib import Path\n"
                 "(Path(sys.argv[1]).parent / 'notes.txt').write_text(''.join('line %d\\n' % i for i in range(5000)))\n")

    def test_cut_short_edit_report_keeps_full_diff_and_before_after_copies(self):
        checked = self.check()
        edited = self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script=self.LONG_EDIT)
        report = edited['change_report']
        self.assertTrue(report['diffs_truncated'])
        self.assertIn('+line 4999', Path(report['full_diff_path']).read_text())
        before, after = Path(report['before_copy_path']), Path(report['after_copy_path'])
        self.assertEqual((before / 'test.kicad_pcb').read_text(), BOARD)
        self.assertFalse((before / 'notes.txt').exists())
        self.assertIn('line 4999', (after / 'notes.txt').read_text())
        # A later edit with a short report keeps the earlier evidence.
        self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                     script="import sys\nfrom pathlib import Path\n(Path(sys.argv[1]).parent / 'notes.txt').write_text('short')\n")
        self.assertIn('line 4999', (after / 'notes.txt').read_text())
        self.assertTrue((before / 'test.kicad_pcb').is_file())
        # The before copy is the edit's own snapshot, moved rather than duplicated.
        self.assertEqual(list((self.settings.work_root / checked['work_copy_id']).glob('snapshot-*')), [])

    def test_full_diff_reads_to_the_end_through_reference_reading(self):
        checked = self.check()
        report = self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                              script=self.LONG_EDIT)['change_report']
        text, offset = '', 0
        while True:
            page = self.success('read_reference_file', path=report['full_diff_path'], offset=offset)
            text += page['text']
            offset += len(page['text'])
            if not page['truncated']:
                break
        self.assertEqual(text, Path(report['full_diff_path']).read_text())
        self.assertGreater(offset, 24000)
        listed = self.success('read_reference_file', path=report['before_copy_path'])
        self.assertIn('test.kicad_pcb', [e['name'] for e in listed['entries']])
        self.failure('arguments_unusable', 'read_reference_file', path=report['before_copy_path'], offset=1)
        # Elsewhere in AL/X storage nothing is readable, including the work copy itself.
        root = Path(report['full_diff_path']).parents[2]
        for path in (root / 'metadata.json', root / 'project' / 'test.kicad_pcb', root.parent):
            self.failure('path_denied', 'read_reference_file', path=str(path))

    def test_retained_evidence_does_not_count_towards_project_limits(self):
        checked = self.check()
        self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script=self.LONG_EDIT)
        root = self.settings.work_root / checked['work_copy_id']
        project_bytes = sum(p.stat().st_size for p in (root / 'project').rglob('*') if p.is_file())
        with patch('alx.providers.kicad.MAX_PROJECT_BYTES', project_bytes + 20000):
            edited = self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                                  script=self.LONG_EDIT.replace("'line", "'row"))
            self.assertTrue(edited['change_report']['diffs_truncated'])
            self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'],
                         script="print('unchanged')\n")

    def test_evidence_over_its_own_limit_undoes_the_edit(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        with patch('alx.providers.kicad.MAX_EVIDENCE_BYTES', 1000):
            result = self.call('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script=self.LONG_EDIT)
        self.assertEqual(result.failure['code'], 'evidence_full')
        self.assertEqual(result.failure['recovery']['rollback_status'], 'restored')
        self.assertFalse((root / 'project' / 'notes.txt').exists())
        self.assertEqual(list((root / 'evidence').iterdir()), [])
        self.success('edit_kicad_work_copy', work_copy_id=checked['work_copy_id'], script="print('ok')\n")

    def test_job_file_is_found_before_renames(self):
        checked = self.check()
        def runner(argv, **kwargs):
            if argv[2] == 'drc':
                return self.run_cli(argv, **kwargs)
            target = Path(argv[argv.index('--output') + 1])
            if argv[3] == 'gerbers':
                (target / 'test-F_Cu.gbr').write_text('X1Y2D01*\n')
                (target / 'test-B_Cu.gbr').write_text('X3Y4D01*\n')
                (target / 'test-job.gbrjob').write_text(json.dumps({'FilesAttributes': [
                    {'Path': 'test-F_Cu.gbr'}, {'Path': 'test-B_Cu.gbr'}]}))
            else:
                (target / 'test.drl').write_text('M48\n')
            return SimpleNamespace(returncode=0)
        runtime = build_kicad_runtime(lambda: 'export', self.environment, self.storage, runner)
        result = runtime.executors['export_kicad_manufacturing']({
            'work_copy_id': checked['work_copy_id'], 'output_dir': 'production',
            'renames': {'test-job.gbrjob': 'maker.json', 'test-F_Cu.gbr': 'top.gbr',
                        'test-B_Cu.gbr': 'bottom.gbrjob'}})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        dest = self.project / 'production'
        job = json.loads((dest / 'maker.json').read_text())
        self.assertEqual([e['Path'] for e in job['FilesAttributes']], ['top.gbr', 'bottom.gbrjob'])
        self.assertEqual((dest / 'bottom.gbrjob').read_text(), 'X3Y4D01*\n')

    def test_failed_export_keeps_digests_of_completed_files(self):
        checked = self.check()
        runtime = self.manufacturing_runtime()
        real_fsync, calls = os.fsync, []
        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('disk full')
            return real_fsync(fd)
        with patch('alx.providers.kicad.os.fsync', fsync):
            result = runtime.executors['export_kicad_manufacturing']({
                'work_copy_id': checked['work_copy_id'], 'output_dir': 'production'})
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        metadata = json.loads((self.storage / 'kicad' / checked['work_copy_id'] / 'metadata.json').read_text())
        self.assertEqual(len(metadata['exports']), 1)
        (name, digest), = metadata['exports'].items()
        import hashlib
        self.assertEqual(hashlib.sha256((self.project / name).read_bytes()).hexdigest(), digest)

    def test_edit_timeout_restores_before_return_and_unstoppable_writer_taints(self):
        from alx.contracts.coding import CodingError
        for unstoppable in (False, True):
            checked = self.check()
            root = self.settings.work_root / checked['work_copy_id']
            initial = inventory(root / 'project')
            def execute(executable, script, board):
                Path(board).write_text(BOARD + '\n')
                if unstoppable:
                    raise CodingError('session_interrupted', reason_code='session_unstoppable')
                raise subprocess.TimeoutExpired('python', 120)
            provider = KiCadProvider(self.settings, self.execute_cli, execute)
            with self.assertRaises(KiCadError) as caught:
                provider.edit({'work_copy_id': checked['work_copy_id'], 'script': 'pass'})
            self.assertEqual(inventory(root / 'project'), initial)
            self.assertEqual(caught.exception.recovery['work_copy_tainted'], unstoppable)
            self.assertEqual(bool(json.loads((root / 'metadata.json').read_text()).get('taint')), unstoppable)

    def test_comparison_preserves_geometry_on_same_line_as_comment(self):
        from alx.providers.kicad import normalized_manufacturing
        self.assertEqual(normalized_manufacturing(b'G04 ignored*X1Y2D01*\n'), ['X1Y2D01*'])
        self.assertNotEqual(normalized_manufacturing(b'G04 ignored*X1Y2D01*\n'),
                            normalized_manufacturing(b'G04 ignored*X3Y4D01*\n'))

    def test_reference_zip_rejects_unsafe_entries_and_reports_truncation(self):
        import zipfile
        archive = self.root / 'unsafe.zip'
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            for name in ('../outside.txt', '/absolute.txt', '.ssh/id_rsa'):
                with zipfile.ZipFile(archive, 'w') as bundle:
                    bundle.writestr(name, 'refused')
                self.failure('path_denied', 'read_reference_file', path=str(archive))
            text = self.root / 'large.txt'
            text.write_text('x' * 25000)
            result = self.success('read_reference_file', path=str(text))
            self.assertTrue(result['truncated'])
            self.assertEqual(len(result['text']), 24000)

    def test_home_icloud_project_is_checkable_without_configured_design_root(self):
        import shutil
        icloud = self.root / 'Library/Mobile Documents/com~apple~CloudDocs/RoomReader'
        shutil.copytree(self.project, icloud)
        with patch('alx.providers.reference_file.Path.home', return_value=self.root):
            result = self.success('check_kicad_board', project_dir=str(icloud))
        self.assertTrue(result['work_copy_id'])

    def test_copy_mutations_are_serialized_across_provider_instances(self):
        checked = self.check()
        provider = KiCadProvider(self.settings, self.execute_cli)
        with provider._locked(checked['work_copy_id']):
            self.failure('work_copy_busy', 'edit_kicad_work_copy',
                         work_copy_id=checked['work_copy_id'], script='pass')

    def test_reference_tilde_path_and_credential_symlink(self):
        with patch.dict('os.environ', {'HOME': str(self.root)}):
            (self.root / 'reference.txt').write_text('reference')
            result = self.success('read_reference_file', path='~/reference.txt')
            self.assertEqual(result['text'], 'reference')
            private = self.root / 'tokens.txt'
            private.write_text('never read')
            (self.root / 'innocent.txt').symlink_to(private)
            self.failure('path_denied', 'read_reference_file', path='~/innocent.txt')


if __name__ == '__main__':
    unittest.main()
