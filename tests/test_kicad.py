"""D-058: isolated board mechanics, bounded files and exact write authority."""
import json
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
from alx.providers.coding_process import run_kicad_cli
from alx.providers.kicad import KiCadProvider, KiCadError, inventory
from alx.safety import AuthorityContext, SafetyGate
from alx.tools.kicad import DEFINITIONS


BOARD = '''(kicad_pcb
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


class KiCadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.design = self.root / 'designs'
        self.project = self.design / 'board'
        self.project.mkdir(parents=True)
        (self.project / 'test.kicad_pcb').write_text(BOARD)
        (self.project / 'test.kicad_pro').write_text(json.dumps(PROJECT))
        self.environment = {
            'ALX_KICAD_DESIGN_ROOTS': str(self.design),
            'ALX_KICAD_TEMPLATE_ROOT': str(self.root / 'templates'),
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
        board = Path(argv[-1])
        self.assertTrue(board.is_relative_to(self.storage))
        self.assertFalse(board.is_relative_to(self.project))
        target = Path(argv[argv.index('--output') + 1])
        if argv[2] == 'drc':
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

    def test_four_unique_catalogue_entries_and_one_runtime_registration(self):
        registry = CapabilityRegistry(self.runtime.definitions)
        self.assertEqual(len(registry.list_definitions()), 4)
        self.assertEqual(set(self.runtime.executors), {d.capability_id for d in DEFINITIONS})
        payload = json.loads(_catalogue_payload(self.runtime.definitions))
        self.assertEqual({d['id'] for d in payload['capabilities']}, set(self.runtime.executors))
        self.assertEqual([d.side_effect for d in DEFINITIONS],
                         [SideEffect.NONE] * 3 + [SideEffect.EFFECTFUL])
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

    def test_mechanical_copy_edits_and_render_counts(self):
        original = inventory(self.project)
        checked = self.check()
        result = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'],
                              silkscreen_min_text=True, ignore_noise_checks=True)
        self.assertEqual((result['before']['total'], result['after']['total']), (2, 1))
        self.assertEqual(inventory(self.project), original)
        copied = self.settings.work_root / checked['work_copy_id'] / 'project'
        board_source = (copied / 'test.kicad_pcb').read_text()
        self.assertEqual(board_source, BOARD.replace('(size 0.5 0.7) (thickness 0.08)',
                                             '(size 1 1) (thickness 0.15)'))
        self.assertEqual(json.loads((copied / 'test.kicad_pro').read_text())['unrelated'], PROJECT['unrelated'])
        self.assertNotEqual(checked['work_copy_revision'], result['work_copy_revision'])
        for path in result['before_images'] + result['after_images']:
            self.assertTrue(Path(path).is_file())

    def test_template_rules_only_and_library_read_only(self):
        folder = self.settings.template_root / 'JLCPCB_4L_1.6mm'
        folder.mkdir(parents=True)
        template = json.loads(json.dumps(PROJECT))
        template['board']['design_settings']['rules']['silk_text_height'] = 1.2
        template['unrelated'] = 'must not copy'
        (folder / 'template.kicad_pro').write_text(json.dumps(template))
        (folder / 'template.kicad_dru').write_text('(version 1)')
        library = self.settings.library_root / 'Test.pretty'
        library.mkdir(parents=True)
        (library / 'Small.kicad_mod').write_text('(footprint "Small" (pad "1" smd rect) (pad "2" smd rect))')
        original_template, original_library = inventory(folder), inventory(library)
        checked = self.check()
        result = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'],
                              apply_template=folder.name)
        copied = self.settings.work_root / result['work_copy_id'] / 'project'
        self.assertEqual((copied / 'test.kicad_dru').read_text(), '(version 1)')
        self.assertEqual(json.loads((copied / 'test.kicad_pro').read_text())['unrelated'], PROJECT['unrelated'])
        found = self.success('find_kicad_library_footprint', designator='U1', work_copy_id=checked['work_copy_id'])
        self.assertEqual(found['candidates'][0]['pad_count'], 2)
        self.assertEqual(found, self.success('find_kicad_library_footprint', library_id='Test:Small'))
        self.assertEqual(inventory(folder), original_template)
        self.assertEqual(inventory(library), original_library)
        self.failure('path_denied', 'apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'], apply_template='../escape')
        self.failure('path_denied', 'find_kicad_library_footprint', library_id='Test:../escape')
        (library / 'Small.kicad_mod').unlink()
        (library / 'Small.kicad_mod').symlink_to(self.project / 'test.kicad_pcb')
        self.failure('path_denied', 'find_kicad_library_footprint', library_id='Test:Small')

    def fixed(self):
        checked = self.check()
        return self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)

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
        self.failure('path_denied', 'apply_kicad_mechanical_fixes', work_copy_id='../outside')
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
                    shell=False, check=False, timeout=120, capture_output=True, text=True)
                self.assertIs(result, execute.return_value)

    def test_cli_os_error_and_timeout_return_unavailable(self):
        for error in (OSError('missing CLI'), subprocess.TimeoutExpired('kicad-cli', 120)):
            with self.subTest(error=type(error).__name__):
                runner = Mock(side_effect=error)
                provider = KiCadProvider(self.settings,
                    lambda executable, arguments: run_kicad_cli(executable, arguments, runner=runner))
                with self.assertRaisesRegex(KiCadError, 'unavailable') as caught:
                    provider._run('pcb', 'drc')
                self.assertIs(caught.exception.__cause__, error)
                runtime = build_kicad_runtime(lambda: 'call', self.environment, self.storage, runner)
                result = runtime.executors['check_kicad_board']({'project_dir': str(self.project)})
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure['code'], 'unavailable')

    def test_custom_noise_configuration_is_the_only_flag_source(self):
        environment = dict(self.environment, ALX_KICAD_NOISE_CHECKS='unconnected_items')
        runtime = build_kicad_runtime(lambda: 'call', environment, self.storage, self.run_cli)
        result = runtime.executors['check_kicad_board']({'project_dir': str(self.project)})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(result.values['violations'][0]['noise'])
        self.assertTrue(result.values['unconnected_items'][0]['noise'])

    def test_routing_and_footprint_mutations_are_refused_even_with_current_revision(self):
        checked = self.check()
        provider = KiCadProvider(self.settings, self.execute_cli)
        root = self.settings.work_root / checked['work_copy_id']
        board = root / 'project' / 'test.kicad_pcb'
        for board_source in (BOARD.replace('(width 0.25)', '(width 0.3)'),
                     BOARD.replace('Test:Small', 'Test:Other'),
                     BOARD.replace('(at 1 2)', '(at 4 5)')):
            board.write_text(board_source)
            args = dict(self.write_args(checked), work_copy_revision=provider._revision(root))
            self.failure('board_structure_changed', 'write_back_kicad_board', **args)
        self.assertEqual((self.project / 'test.kicad_pcb').read_text(), BOARD)

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
        fixed = self.success('apply_kicad_mechanical_fixes',
                             work_copy_id=checked['work_copy_id'],
                             silkscreen_min_text=True, ignore_noise_checks=True)
        return self.write_args(fixed)

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


if __name__ == '__main__':
    unittest.main()
