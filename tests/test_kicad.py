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

    def test_kicad8_silkscreen_optional_nodes_and_visibility(self):
        from alx.providers.kicad import board_invariant, descendants, sexpr
        source = (Path(__file__).parent / 'fixtures/kicad/silkscreen_kicad8.kicad_pcb').read_text()
        (self.project / 'test.kicad_pcb').write_text(source)
        checked = self.check()
        result = self.success('apply_kicad_mechanical_fixes',
                              work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)
        board = self.settings.work_root / checked['work_copy_id'] / 'project/test.kicad_pcb'
        updated = board.read_text()
        self.assertEqual(board_invariant(source), board_invariant(updated))
        originals = [n for n in descendants(sexpr(source))
                     if n.name in ('property', 'fp_text', 'gr_text')]
        texts = [n for n in descendants(sexpr(updated))
                 if n.name in ('property', 'fp_text', 'gr_text')]
        for original, text in zip(originals, texts, strict=True):
            with self.subTest(text=text.children[:3]):
                old = source[original.start:original.end]
                if 'hidden' in old or text.one('layer').children[1] == 'F.Cu':
                    self.assertEqual(updated[text.start:text.end], old)
                    continue
                font = text.one('effects').one('font')
                self.assertTrue(all(float(v) >= 1 for v in font.one('size').children[1:]))
                self.assertGreaterEqual(float(font.one('thickness').children[1]), 0.15)
                if original.named('effects') and original.one('effects').named('font'):
                    old_font = original.one('effects').one('font')
                    old_sizes = (old_font.one('size').children[1:]
                                 if old_font.named('size') else [1, 1])
                    for old_size, size in zip(old_sizes, font.one('size').children[1:], strict=True):
                        self.assertEqual(float(size), max(1, float(old_size)))
                    old_thickness = (float(old_font.one('thickness').children[1])
                                     if old_font.named('thickness') else 0.15)
                    self.assertEqual(float(font.one('thickness').children[1]),
                                     max(0.15, old_thickness))
        self.assertIn('bold', updated)
        self.assertIn('(justify left)', updated)
        again = self.success('apply_kicad_mechanical_fixes',
                             work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)
        self.assertFalse(again['changes'])
        self.assertEqual(again['work_copy_revision'], result['work_copy_revision'])

    def test_kicad7_and_9_silk_fonts_and_dependent_caches(self):
        from alx.providers.kicad import board_invariant, descendants, sexpr, silk_text
        for version in (7, 9):
            with self.subTest(version=version):
                source = (Path(__file__).parent / 'fixtures/kicad' /
                          f'silkscreen_kicad{version}.kicad_pcb').read_text()
                (self.project / 'test.kicad_pcb').write_text(source)
                checked = self.check()
                self.success('apply_kicad_mechanical_fixes',
                             work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)
                board = self.settings.work_root / checked['work_copy_id'] / 'project/test.kicad_pcb'
                updated = board.read_text()
                self.assertEqual(board_invariant(source), board_invariant(updated))
                originals = [n for n in descendants(sexpr(source)) if silk_text(n)]
                texts = [n for n in descendants(sexpr(updated)) if silk_text(n)]
                for original, text in zip(originals, texts, strict=True):
                    old = source[original.start:original.end]
                    if 'hidden' in old:
                        self.assertEqual(updated[text.start:text.end], old)
                        continue
                    font = text.one('effects').one('font')
                    self.assertTrue(all(float(v) >= 1 for v in font.one('size').children[1:]))
                    self.assertGreaterEqual(float(font.one('thickness').children[1]), 0.15)
                    self.assertFalse(text.named('render_cache'))
                self.assertIn('(face "Noto Sans")', updated)
                self.assertIn('bold italic', updated)
                self.assertIn('(justify right mirror)', updated)
                self.assertIn('(tstamp "board-text-id")', updated)
                copper = next(n for n in sexpr(updated).named('gr_text') if not silk_text(n))
                self.assertTrue(copper.named('render_cache'))
                self.assertIn(updated[copper.start:copper.end], source)

    def test_invariant_ignores_only_direct_silk_dimensions_and_caches(self):
        from alx.providers.kicad import board_invariant
        source = ('(kicad_pcb (gr_text "private" (layer "F.SilkS" knockout) '
                  '(effects (font (size 0.5 0.5) (thickness 0.08) bold) (justify mirror)) '
                  '(render_cache "private" 0 (polygon (pts (xy 1 2))))) '
                  '(gr_text "copper" (layer "F.Cu") (render_cache "copper" 0)))')
        changed_cache = source.replace('(xy 1 2)', '(xy 3 4)')
        self.assertEqual(board_invariant(source), board_invariant(changed_cache))
        for before, after in (('bold', 'italic'), ('mirror', 'left'),
                              ('"private" (layer', '"changed" (layer'),
                              ('"copper" 0', '"copper" 1'),
                              ('F.SilkS', 'B.SilkS')):
            with self.subTest(before=before):
                self.assertNotEqual(board_invariant(source), board_invariant(source.replace(before, after)))
        # Similar descendants outside text.effects.font must remain authoritative.
        other = source.replace('(render_cache "private"', '(other (font (size 2 2))) (render_cache "private"')
        self.assertNotEqual(board_invariant(other), board_invariant(other.replace('(size 2 2)', '(size 3 3)')))

    def test_first_difference_is_deterministic_and_sanitised(self):
        from alx.providers.kicad import first_board_difference
        cases = [
            (['kicad_pcb', ['gr_text', 'secret']], ['kicad_pcb', ['gr_text', 'other']],
             ['kicad_pcb', 1, 'gr_text', 1]),
            (['kicad_pcb', ['secret', 'value']], ['kicad_pcb', ['secret', 'changed']],
             ['kicad_pcb', 1, 'unknown', 1]),
            (['kicad_pcb'], ['kicad_pcb', ['secret', 'value']],
             ['kicad_pcb', 1, 'unknown']),
            (['kicad_pcb', 'secret'], ['kicad_pcb', ['gr_text', 'value']],
             ['kicad_pcb', 1, 'gr_text']),
            (['kicad_pcb', ['pad']], ['kicad_pcb', ['segment']],
             ['kicad_pcb', 1, 'pad', 0]),
        ]
        for expected, current, path in cases:
            self.assertEqual(first_board_difference(expected, current), path)
            self.assertEqual(first_board_difference(current, current), None)

    def test_drc_invariant_failure_reports_one_private_safe_path_and_rolls_back(self):
        checked = self.check()
        run_cli = self.run_cli
        drcs = 0
        def changing(argv, **kwargs):
            nonlocal drcs
            result = run_cli(argv, **kwargs)
            if argv[2] == 'drc':
                drcs += 1
                if drcs == 2:
                    board = Path(argv[-1])
                    board.write_text(board.read_text().replace('"copper"', '"private-board-text"'))
            return result
        self.runtime = build_kicad_runtime(lambda: 'call', self.environment, self.storage, changing)
        with self.assertLogs('alx.providers.kicad', level='WARNING') as logs:
            result = self.call('apply_kicad_mechanical_fixes',
                               work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)
        self.assertEqual(result.failure['code'], 'board_structure_changed')
        path = ['kicad_pcb', 2, 'gr_text', 1]
        self.assertEqual(list(result.failure['recovery']['first_difference_path']), path)
        self.assertEqual(result.failure['recovery']['rollback_status'], 'restored')
        self.assertEqual(len(logs.records), 1)
        self.assertIn(json.dumps(path), logs.output[0])
        self.assertNotIn('private-board-text', str(result.failure) + logs.output[0])
        self.assertNotIn('copper', str(result.failure) + logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)
        board = self.settings.work_root / checked['work_copy_id'] / 'project/test.kicad_pcb'
        self.assertEqual(board.read_text(), BOARD)

    def test_missing_silk_minima_report_unsupported_project_details(self):
        for missing in (['silk_text_height'], ['silk_text_thickness'],
                        ['silk_text_height', 'silk_text_thickness']):
            with self.subTest(missing=missing):
                project = json.loads(json.dumps(PROJECT))
                for key in missing:
                    del project['board']['design_settings']['rules'][key]
                (self.project / 'test.kicad_pro').write_text(json.dumps(project))
                checked = self.check()
                root = self.settings.work_root / checked['work_copy_id']
                before = inventory(root / 'project')
                result = self.call('apply_kicad_mechanical_fixes',
                                   work_copy_id=checked['work_copy_id'], silkscreen_min_text=True)
                self.assertEqual(result.failure['code'], 'unsupported_project')
                recovery = result.failure['recovery']
                self.assertEqual(list(recovery['missing_settings']), missing)
                self.assertEqual(recovery['rollback_status'], 'restored')
                self.assertEqual(inventory(root / 'project'), before)

    def test_unexpected_apply_error_reports_only_sanitised_type_and_stage(self):
        from alx.providers import kicad
        for stage in ('before_drc', 'render', 'template', 'clearance', 'noise',
                      'silkscreen', 'after_drc', 'revision', 'metadata'):
            with self.subTest(stage=stage):
                checked = self.check()
                provider = KiCadProvider(self.settings, self.execute_cli)
                root, meta = provider._load(checked['work_copy_id'])
                before = inventory(root / 'project')
                error = RuntimeError('unexpected fixture failure')
                options = dict(work_copy_id=checked['work_copy_id'], silkscreen_min_text=True,
                               ignore_noise_checks=True, same_footprint_pad_clearance_mm=0.1)
                if stage in ('before_drc', 'after_drc'):
                    drc = provider._drc
                    calls = 0
                    def fail_drc(*args):
                        nonlocal calls
                        calls += 1
                        if calls == (1 if stage == 'before_drc' else 2):
                            raise error
                        return drc(*args)
                    failing = patch.object(provider, '_drc', side_effect=fail_drc)
                elif stage == 'render':
                    failing = patch.object(provider, '_render', side_effect=error)
                elif stage == 'revision':
                    failing = patch.object(provider, '_revision', side_effect=error)
                elif stage == 'metadata':
                    save = provider._save_metadata
                    saves = 0
                    def fail_final_save(*args):
                        nonlocal saves
                        saves += 1
                        # Allow the pending marker and subsequent rollback save.
                        if saves == 2:
                            raise error
                        return save(*args)
                    failing = patch.object(provider, '_save_metadata', side_effect=fail_final_save)
                elif stage == 'template':
                    failing = patch.object(kicad.json, 'loads', side_effect=error)
                    # Avoid failing in _load, before the apply transaction starts.
                    provider._load = lambda _: (root, meta)
                elif stage == 'clearance':
                    failing = patch.object(kicad, 'sexpr', side_effect=error)
                elif stage == 'noise':
                    failing = patch.object(provider, 'settings',
                        noise_checks=Mock(__iter__=Mock(side_effect=error)))
                else:
                    options.pop('same_footprint_pad_clearance_mm')
                    failing = patch.object(kicad, 'sexpr', side_effect=error)
                # Parser/json patches must not affect the before DRC's inspection.
                drc_report = provider._drc(root, meta)
                with patch.object(provider, '_drc', return_value=drc_report) if stage not in (
                        'before_drc', 'after_drc') else patch.object(provider, '_render', wraps=provider._render):
                    with patch.object(provider, '_render', wraps=provider._render) as render, \
                            failing, self.assertLogs(kicad.logger, level='WARNING') as logs:
                        from alx.tools.kicad import build_kicad_executors
                        result = build_kicad_executors(provider, lambda: 'call')[
                            'apply_kicad_mechanical_fixes'](options)
                if stage in ('revision', 'metadata'):
                    self.assertEqual(render.call_count, 2)
                self.assertEqual(result.failure['code'], 'verification_failed')
                recovery = result.failure['recovery']
                self.assertEqual(recovery['error'], dict(code='verification_failed',
                    type='RuntimeError', stage=stage))
                self.assertIn(stage, logs.output[0])
                self.assertIn('RuntimeError', logs.output[0])
                self.assertNotIn(str(error), '\n'.join(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.assertEqual(recovery['rollback_status'], 'restored')
                self.assertFalse(recovery['work_copy_tainted'])
                self.assertEqual(inventory(root / 'project'), before)
                self.assertEqual(provider._load(checked['work_copy_id'])[1], meta)

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
                    self.assertIn('export stalled', logs.output[0])
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

    def test_same_footprint_clearance_validation_before_cli_or_edits(self):
        checked = self.check()
        count = len(self.calls)
        for invalid in (True, False, None, '0.1', 0, -1, float('nan'), float('inf'), -float('inf'), 10 ** 1000):
            self.failure('arguments_unusable', 'apply_kicad_mechanical_fixes',
                         work_copy_id=checked['work_copy_id'], same_footprint_pad_clearance_mm=invalid)
        self.assertEqual(len(self.calls), count)

    def test_svg_timeout_returns_distinct_capability_failure(self):
        checked = self.check()
        def runner(argv, **kwargs):
            if argv[1:4] == ['pcb', 'export', 'svg']:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'], stderr=b'SVG stalled')
            return self.run_cli(argv, **kwargs)
        runtime = build_kicad_runtime(lambda: 'call', self.environment, self.storage, runner)
        with self.assertLogs('alx.providers.kicad', level='WARNING') as logs:
            result = runtime.executors['apply_kicad_mechanical_fixes']({'work_copy_id': checked['work_copy_id']})
        self.assertEqual(result.failure['code'], 'cli_timeout')
        self.assertIn('pcb export svg timed out', logs.output[0])
        self.assertIn('SVG stalled', logs.output[0])

    def test_same_footprint_rule_creation_update_and_template_coexistence(self):
        checked = self.check()
        options = dict(work_copy_id=checked['work_copy_id'], same_footprint_pad_clearance_mm=0.1)
        target = self.settings.work_root / checked['work_copy_id'] / 'project/test.kicad_dru'
        first = self.success('apply_kicad_mechanical_fixes', **options)
        self.assertIn({'kind': 'same_footprint_pad_clearance', 'value': '0.1', 'clearance_mm': 0.1}, first['changes'])
        saved = target.read_bytes()
        second = self.success('apply_kicad_mechanical_fixes', **options)
        self.assertEqual(target.read_bytes(), saved)
        self.assertFalse(second['changes'])
        folder = self.root / 'templates/factory'
        folder.mkdir(parents=True)
        (folder / 'factory.kicad_pro').write_text(json.dumps(PROJECT))
        template_rules = '# Keep this comment\n(version 1)\n(rule "factory" (constraint clearance (min 0.2mm)))\n'
        (folder / 'factory.kicad_dru').write_text(template_rules)
        self.success('apply_kicad_mechanical_fixes', **dict(options, apply_template='factory'))
        self.assertTrue(target.read_text().startswith(template_rules))
        self.success('apply_kicad_mechanical_fixes', **dict(options, same_footprint_pad_clearance_mm=0.15))
        self.assertEqual(target.read_text().count('(rule "ALX same-footprint pad clearance"'), 1)
        self.assertIn('(min 0.15mm)', target.read_text())
        # Repair duplicate managed rules while retaining unrelated existing rules.
        managed_rule = target.read_text()[target.read_text().index('(rule "ALX'):]
        target.write_text(target.read_text() + managed_rule)
        self.success('apply_kicad_mechanical_fixes', **options)
        self.assertEqual(target.read_text().count('(rule "ALX'), 1)
        self.assertIn('(rule "factory"', target.read_text())
        self.assertFalse((self.project / 'test.kicad_dru').exists())

    def template(self, rules='(version 1)\n'):
        folder = self.settings.template_root / 'factory'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'factory.kicad_pro').write_text(json.dumps(PROJECT))
        (folder / 'factory.kicad_dru').write_text(rules)
        return folder.name

    def test_apply_failure_restores_every_file_and_original_absence(self):
        for existing_rules in (False, True):
            for phase in ('before_drc', 'after_drc', 'after_render'):
                for timeout in (False, True):
                    with self.subTest(existing_rules=existing_rules, phase=phase, timeout=timeout):
                        checked = self.check()
                        root = self.settings.work_root / checked['work_copy_id']
                        project = root / 'project'
                        if existing_rules:
                            (project / 'test.kicad_dru').write_bytes(b'# original rules\r\n(version 1)\r\n')
                        before = {p.name: p.read_bytes() for p in project.iterdir()}
                        provider = KiCadProvider(self.settings, self.execute_cli)
                        revision = provider._revision(root)
                        drcs = renders = 0
                        def runner(executable, argv):
                            nonlocal drcs, renders
                            is_drc = argv[1] == 'drc'
                            drcs += is_drc
                            renders += not is_drc
                            fail = ((phase == 'before_drc' and drcs == 1 and is_drc)
                                    or (phase == 'after_drc' and drcs == 2 and is_drc)
                                    or (phase == 'after_render' and renders == 2 and not is_drc))
                            if fail:
                                # Even preparation/refill can mutate all destinations.
                                for suffix in ('.kicad_pcb', '.kicad_pro', '.kicad_dru'):
                                    (project / ('test' + suffix)).write_bytes(b'partial CLI write')
                                if timeout:
                                    raise subprocess.TimeoutExpired(argv, 1)
                                return SimpleNamespace(returncode=1)
                            return self.execute_cli(executable, argv)
                        provider.execute_cli = runner
                        with self.assertRaises(KiCadError) as caught:
                            provider.fix(dict(work_copy_id=checked['work_copy_id'],
                                              apply_template=self.template(),
                                              silkscreen_min_text=True, ignore_noise_checks=True))
                        self.assertEqual(str(caught.exception), 'cli_timeout' if timeout else 'cli_failed')
                        self.assertEqual(caught.exception.recovery['rollback_status'], 'restored')
                        self.assertEqual({p.name: p.read_bytes() for p in project.iterdir()}, before)
                        self.assertEqual(provider._revision(root), revision)
                        self.assertNotIn('taint', provider._load(checked['work_copy_id'])[1])

    def test_apply_shutdown_restores_snapshot_and_propagates_through_capability(self):
        from alx.tools.kicad import build_kicad_executors
        for exception_type in (KeyboardInterrupt, SystemExit):
            for state in ('clean', 'previously_tainted', 'failed_restore'):
                with self.subTest(exception_type=exception_type, state=state):
                    checked = self.check()
                    root = self.settings.work_root / checked['work_copy_id']
                    project = root / 'project'
                    provider = KiCadProvider(self.settings, self.execute_cli)
                    meta = provider._load(checked['work_copy_id'])[1]
                    if state == 'previously_tainted':
                        meta['taint'] = 'prior-taint'
                        provider._save_metadata(root, meta)
                    before = inventory(project)
                    revision = provider._revision(root)
                    shutdown = exception_type('stop apply')
                    drcs = 0
                    def execute(executable, argv):
                        nonlocal drcs
                        if argv[1] == 'drc':
                            drcs += 1
                            if drcs == 2:
                                for suffix in ('.kicad_pcb', '.kicad_pro', '.kicad_dru'):
                                    (project / ('test' + suffix)).write_bytes(b'partial write')
                                raise shutdown
                        return self.execute_cli(executable, argv)
                    provider.execute_cli = execute
                    write = Path.write_bytes
                    def restore(path, data):
                        if (state == 'failed_restore' and path == project / 'test.kicad_pro'
                                and data != b'partial write'):
                            raise PermissionError('rollback refused')
                        return write(path, data)
                    executor = build_kicad_executors(provider, lambda: 'call')['apply_kicad_mechanical_fixes']
                    with patch.object(Path, 'write_bytes', restore):
                        with self.assertRaises(BaseException) as caught:
                            executor(dict(work_copy_id=checked['work_copy_id'],
                                          silkscreen_min_text=True,
                                          same_footprint_pad_clearance_mm=0.1))
                    self.assertIs(caught.exception, shutdown)
                    restarted = KiCadProvider(self.settings, self.execute_cli)
                    persisted = restarted._load(checked['work_copy_id'])[1]
                    if state == 'failed_restore':
                        self.assertTrue(persisted['taint'])
                        self.assertNotEqual(restarted._revision(root), revision)
                        # Repair only the malformed project so DRC can inspect taint.
                        (project / 'test.kicad_pro').write_text(json.dumps(PROJECT))
                        report = restarted._drc(root, persisted)
                        self.assertTrue(report['work_copy_tainted'])
                        self.assertFalse(report['baseline_eligible'])
                    else:
                        self.assertEqual(inventory(project), before)
                        self.assertEqual(persisted, meta)
                        self.assertEqual(restarted._revision(root), revision)

    def test_before_timeout_removes_initially_absent_project_and_rules(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        project = root / 'project'
        (project / 'test.kicad_pro').unlink()
        before = inventory(project)
        def execute(executable, argv):
            (project / 'test.kicad_pro').write_text('{}')
            (project / 'test.kicad_dru').write_text('(version 1)')
            raise subprocess.TimeoutExpired(argv, 1)
        provider = KiCadProvider(self.settings, execute)
        with self.assertRaisesRegex(KiCadError, 'cli_timeout'):
            provider.fix({'work_copy_id': checked['work_copy_id']})
        self.assertEqual(inventory(project), before)

    def test_rollback_readback_detects_silent_corruption(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        pro = root / 'project/test.kicad_pro'
        write = Path.write_bytes
        def corrupt(path, data):
            return write(path, b'corrupted restoration' if path == pro else data)
        provider = KiCadProvider(self.settings, self.execute_cli)
        with patch.object(provider, '_render', side_effect=KiCadError('render_failed')), \
                patch.object(Path, 'write_bytes', corrupt):
            with self.assertRaises(KiCadError) as caught:
                provider.fix({'work_copy_id': checked['work_copy_id']})
        self.assertEqual(caught.exception.recovery['rollback_status'], 'failed')
        self.assertTrue(provider._load(checked['work_copy_id'])[1]['taint'])

    def test_real_timed_out_writer_is_stopped_before_rollback(self):
        from threading import Event
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        project = root / 'project'
        before = inventory(project)
        dru = project / 'test.kicad_dru'
        ready = root / 'artifacts/writer-ready'
        # A descendant ignores TERM and continually writes until group cleanup kills it.
        child = ("import signal, time; from pathlib import Path; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 f"Path({str(ready)!r}).write_text('ready'); "
                 f"p=Path({str(dru)!r}); "
                 "exec(\"while True:\\n p.write_bytes(b'late writer')\\n time.sleep(0.02)\")")
        leader = ("import subprocess, sys, time; "
                  f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)")
        drcs = 0
        def execute(executable, argv):
            nonlocal drcs
            if argv[1] == 'drc':
                drcs += 1
                if drcs == 2:
                    return run_kicad_cli(sys.executable, ['-c', leader], timeout_seconds=1)
            return self.execute_cli(executable, argv)
        provider = KiCadProvider(self.settings, execute)
        with self.assertRaisesRegex(KiCadError, 'cli_timeout'):
            provider.fix(dict(work_copy_id=checked['work_copy_id'],
                              apply_template=self.template(), silkscreen_min_text=True))
        self.assertTrue(ready.exists(), 'descendant must actually start before timeout')
        self.assertEqual(inventory(project), before)
        Event().wait(0.2)
        self.assertEqual(inventory(project), before, 'writer must not overwrite restored files')

    def test_failed_restoration_is_durable_and_never_a_baseline(self):
        checked = self.check()
        root = self.settings.work_root / checked['work_copy_id']
        board = root / 'project/test.kicad_pcb'
        provider = KiCadProvider(self.settings, self.execute_cli)
        revision = provider._revision(root)
        original_write = Path.write_bytes
        def blocked_restore(path, data):
            if path == board:
                raise PermissionError('rollback refused')
            return original_write(path, data)
        with patch.object(provider, '_render', side_effect=KiCadError('render_failed')), \
                patch.object(Path, 'write_bytes', blocked_restore):
            with self.assertRaises(KiCadError) as caught:
                provider.fix({'work_copy_id': checked['work_copy_id']})
        self.assertTrue(caught.exception.recovery['work_copy_tainted'])
        self.assertFalse(caught.exception.recovery['baseline_eligible'])
        restarted = KiCadProvider(self.settings, self.execute_cli)
        self.assertNotEqual(restarted._revision(root), revision)
        result = restarted.fix({'work_copy_id': checked['work_copy_id']})
        self.assertTrue(result['work_copy_tainted'])
        for report in (result, result['before'], result['after']):
            self.assertFalse(report['baseline_eligible'])
        self.assertTrue(restarted._load(checked['work_copy_id'])[1]['taint'])

    def test_unstoppable_writer_keeps_taint_even_after_byte_restoration(self):
        from alx.contracts.coding import CodingError
        checked = self.check()
        provider = KiCadProvider(self.settings, self.execute_cli)
        with patch.object(provider, '_render', side_effect=CodingError(
                'session_interrupted', reason_code='session_unstoppable')):
            with self.assertRaises(KiCadError) as caught:
                provider.fix({'work_copy_id': checked['work_copy_id']})
        self.assertTrue(caught.exception.recovery['work_copy_tainted'])
        self.assertFalse(caught.exception.recovery['baseline_eligible'])

    def test_existing_equivalent_condition_is_preserved_and_named(self):
        from alx.providers.kicad import SAME_PART_CONDITION
        for template in (False, True):
            checked = self.check()
            root = self.settings.work_root / checked['work_copy_id']
            rules = ('# Preserve this rule and its constraint\n(version 1)\n'
                     '(rule "Same part pads" (condition ' + json.dumps(
                         SAME_PART_CONDITION.replace(' == ', '==').replace(' && ', '  &&  '))
                     + ') (constraint clearance (min 0.12mm)))\n')
            target = root / 'project/test.kicad_dru'
            options = {}
            if template:
                options['apply_template'] = self.template(rules)
            else:
                target.write_text(rules)
            result = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'],
                                  same_footprint_pad_clearance_mm=0.1, **options)
            self.assertEqual(target.read_text(), rules)
            self.assertNotIn('ALX same-footprint', target.read_text())
            self.assertIn({'kind': 'same_footprint_pad_clearance', 'value': 'Same part pads',
                           'status': 'already_present', 'rule_name': 'Same part pads'}, result['changes'])

    def test_condition_comparison_preserves_literal_spaces_and_operators(self):
        from alx.providers.kicad import SAME_PART_CONDITION
        for condition in (SAME_PART_CONDITION.replace("'Pad'", "'P ad'"),
                          SAME_PART_CONDITION.replace('&&', '||'),
                          SAME_PART_CONDITION.replace('A.Parent ==', 'A.Parent !=')):
            checked = self.check()
            target = self.settings.work_root / checked['work_copy_id'] / 'project/test.kicad_dru'
            target.write_text('(version 1)\n(rule "Different" (condition ' + json.dumps(condition)
                              + ') (constraint clearance (min 0.2mm)))\n')
            self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'],
                         same_footprint_pad_clearance_mm=0.1)
            self.assertIn('ALX same-footprint', target.read_text())

    def test_rule_citations_cover_all_violations_and_apply_reports(self):
        finding = {'type': 'clearance', 'severity': 'error', 'items': [],
                   'description': "Clearance violation (rule 'Same part pads'; clearance 0.1 mm)"}
        self.report = {'violations': [finding] * 12 + [dict(finding,
            description='Clearance violation (rule "Other rule"; clearance 0.2 mm)')],
            'unconnected_items': [], 'schematic_parity': []}
        checked = self.check()
        expected = [{'name': 'Other rule', 'count': 1}, {'name': 'Same part pads', 'count': 12}]
        self.assertEqual([dict(c) for c in checked['custom_rule_citations']], expected)
        fixed = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'])
        for report in (fixed['before'], fixed['after']):
            self.assertEqual([dict(c) for c in report['custom_rule_citations']], expected)
        self.report['violations'] = []
        checked = self.check()
        self.assertFalse(checked['custom_rule_citations'])
        fixed = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'])
        self.assertFalse(fixed['before']['custom_rule_citations'])
        self.assertFalse(fixed['after']['custom_rule_citations'])

    def test_rule_citations_preserve_embedded_quotes_and_distinct_prefixes(self):
        names = ['U1', "U1's pads", "U1's pads extended", 'U1 "signal" pads',
                 '''U1's "signal" pads''']
        descriptions = []
        expected = []
        for index, name in enumerate(names, 1):
            expected.append({'name': name, 'count': index * 8})
            for quote in ("'", '"'):
                for suffix in ('; clearance 0.1 mm)', ')', '',
                               ' clearance 0.1000 mm; actual 0.0500 mm)'):
                    citation = f'rule {quote}{name}{quote}'
                    # Repeated citations in one finding still count only once.
                    descriptions.extend([f'{citation}{suffix}; also {citation}{suffix}'] * index)
        finding = {'type': 'clearance', 'severity': 'error', 'items': []}
        self.report = {'violations': [dict(finding, description=text) for text in descriptions],
                       'unconnected_items': [], 'schematic_parity': []}
        checked = self.check()
        expected.sort(key=lambda citation: citation['name'])
        self.assertEqual([dict(c) for c in checked['custom_rule_citations']], expected)
        fixed = self.success('apply_kicad_mechanical_fixes', work_copy_id=checked['work_copy_id'])
        for report in (fixed['before'], fixed['after']):
            self.assertEqual([dict(c) for c in report['custom_rule_citations']], expected)

    def test_rule_citations_match_loaded_names_and_full_fallback_boundaries(self):
        names = ['U1', "U1's pads", "U1' pads", "U1' tracks",
                 "U1' pads' extended", 'U1" pads', 'U1" tracks',
                 'U1" pads" extended', "U1'; pads", "U1'; tracks",
                 'U1"); pads', 'U1"); tracks']
        finding = {'type': 'clearance', 'severity': 'error', 'items': []}
        descriptions = []
        for index, name in enumerate(names, 1):
            for quote in ("'", '"'):
                for suffix in ('; clearance 0.1 mm)', ')', '',
                               ' clearance 0.1000 mm; actual 0.0500 mm)'):
                    citation = f'rule {quote}{name}{quote}{suffix}'
                    descriptions.extend([f'{citation}; also {citation}'] * index)
        self.report = {'violations': [dict(finding, description=text) for text in descriptions],
                       'unconnected_items': [], 'schematic_parity': []}
        expected = sorted([{'name': name, 'count': index * 8}
                           for index, name in enumerate(names, 1)], key=lambda c: c['name'])
        # Both absent rules and unrelated loaded rules must use the full fallback.
        for loaded_names in ([], ['Unrelated'], names):
            with self.subTest(loaded_names=loaded_names):
                (self.project / 'test.kicad_dru').write_text('(version 1)\n' + '\n'.join(
                    f'(rule {json.dumps(name)} (constraint clearance (min 0.1mm)))'
                    for name in loaded_names))
                checked = self.check()
                self.assertEqual([dict(c) for c in checked['custom_rule_citations']], expected)
                fixed = self.success('apply_kicad_mechanical_fixes',
                                     work_copy_id=checked['work_copy_id'])
                for report in (fixed['before'], fixed['after']):
                    self.assertEqual([dict(c) for c in report['custom_rule_citations']], expected)

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
