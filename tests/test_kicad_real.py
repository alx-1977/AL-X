"""Real CLI contract: only KiCad's upgraded output reaches the provider."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.contracts import CapabilityResultState
from alx.providers.coding_process import run_kicad_cli
import json
import time

FIXTURE = Path(__file__).parent / 'fixtures' / 'kicad'
CLI = shutil.which('kicad-cli')
if not CLI:
    mac_cli = Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')
    if mac_cli.is_file():
        CLI = str(mac_cli)


class CapturedKiCadTests(unittest.TestCase):
    def test_captured_10_0_7_report_through_provider(self):
        # Replay actual CLI bytes, independent of the provider's flag/report mocks.
        def captured(argv, **kwargs):
            from types import SimpleNamespace
            shutil.copy2(FIXTURE / 'drc-10.0.7.json',
                         argv[argv.index('--output') + 1])
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'designs' / 'minimal'
            project.mkdir(parents=True)
            for suffix in ('.kicad_pcb', '.kicad_pro'):
                shutil.copy2(FIXTURE / ('minimal' + suffix), project)
            runtime = build_kicad_runtime(lambda: 'captured', {
                'ALX_KICAD_DESIGN_ROOTS': str(root / 'designs'),
            }, root / 'storage', captured)
            result = runtime.executors['check_kicad_board']({'project_dir': str(project)})
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            self.assertEqual(result.values['totals'],
                             {'error': 0, 'warning': 2, 'ignore': 0, 'total': 2})
            self.assertEqual({g['check_type'] for g in result.values['violations']},
                             {'text_height', 'text_thickness'})
            for group in result.values['violations']:
                item = group['findings'][0]['items'][0]
                self.assertEqual(item['position'], {'x': 105.0, 'y': 105.0})
                self.assertEqual(item['uuid'], 'd8478024-7b53-4e3c-b624-b00426b46aed')


@unittest.skipUnless(CLI, 'kicad-cli 9 or later is required')
class RealKiCadTests(unittest.TestCase):
    def test_same_footprint_clearance_is_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'designs/pads'
            project.mkdir(parents=True)
            board = (FIXTURE / 'minimal.kicad_pcb').read_text()
            footprints = []
            # U1: 0.15 mm gap; U2: overlap; U3/U4: different parents; U5: 0.05 mm gap.
            for reference, x, y, positions in (('U1', 102, 102, (0, 1.15)),
                                               ('U2', 106, 102, (0, 0.9)),
                                               ('U3', 102, 106, (0,)),
                                               ('U4', 103.15, 106, (0,)),
                                               ('U5', 106, 106, (0, 1.05))):
                pads = ''.join(f'(pad "{i}" smd rect (at {offset} 0) (size 1 1) '
                               f'(layers "F.Cu") (net {i} "N{i}"))'
                               for i, offset in enumerate(positions, 1))
                if reference == 'U4':
                    pads = pads.replace('(net 1 "N1")', '(net 2 "N2")')
                footprints.append(f'(footprint "Test:Pads" (layer "F.Cu") (at {x} {y}) '
                                  f'(property "Reference" "{reference}" (at 0 -1) (layer "F.SilkS")) {pads})')
            board = board.rstrip()[:-1] + '(net 1 "N1")(net 2 "N2")' + ''.join(footprints) + ')'
            (project / 'pads.kicad_pcb').write_text(board)
            pro = json.loads((FIXTURE / 'minimal.kicad_pro').read_text())
            pro['board']['design_settings']['rules']['min_clearance'] = 0.2
            (project / 'pads.kicad_pro').write_text(json.dumps(pro))
            timings = []
            def measured(executable, arguments, **kwargs):
                started = time.monotonic()
                result = run_kicad_cli(executable, arguments, **kwargs)
                timings.append((arguments[:3], time.monotonic() - started))
                return result
            with patch('alx.bootstrap.kicad.run_kicad_cli', measured):
                runtime = build_kicad_runtime(lambda: 'pads', {
                    'ALX_KICAD_CLI': CLI, 'ALX_KICAD_DESIGN_ROOTS': str(root / 'designs')}, root / 'storage')
                checked = runtime.executors['check_kicad_board']({'project_dir': str(project)})
                self.assertEqual(checked.state, CapabilityResultState.SUCCEEDED, checked.failure)
                before = next(g for g in checked.values['violations'] if g['check_type'] == 'clearance')
                self.assertEqual(before['count'], 3, checked.values['violations'])
                fixed = runtime.executors['apply_kicad_mechanical_fixes']({
                    'work_copy_id': checked.values['work_copy_id'], 'same_footprint_pad_clearance_mm': 0.1})
                self.assertEqual(fixed.state, CapabilityResultState.SUCCEEDED, fixed.failure)
                # U1 is resolved; overlap, different parents and the real U5 fault remain.
                reports = list((root / 'storage/kicad' / checked.values['work_copy_id'] / 'artifacts').glob('*.json'))
                latest = max(reports, key=lambda p: p.stat().st_mtime_ns)
                violations = json.loads(latest.read_text())['violations']
                clearance = [v for v in violations if v['type'] == 'clearance']
                self.assertEqual(len(clearance), 2, violations)
                self.assertEqual([dict(c) for c in fixed.values['after']['custom_rule_citations']],
                                 [{'name': 'ALX same-footprint pad clearance', 'count': 1}])
                self.assertTrue(any(v['type'] == 'shorting_items' and 'U2' in str(v) for v in violations))
                self.assertFalse(any('U1' in str(v) for v in clearance))
                self.assertFalse(any(v['type'] == 'dsl_parse_error' for v in violations), violations)
                self.assertTrue(fixed.values['before_images'])
                self.assertTrue(fixed.values['after_images'])
            print('KiCad fixture subcommand seconds:', timings)

    def test_saved_project_drc_and_svg(self):
        cli = CLI
        fixture = FIXTURE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'designs' / 'minimal'
            project.mkdir(parents=True)
            for suffix in ('.kicad_pcb', '.kicad_pro'):
                shutil.copy2(fixture / ('minimal' + suffix), project)
            before = {p.name: p.read_bytes() for p in project.iterdir()}
            runtime = build_kicad_runtime(lambda: 'real-kicad', {
                'ALX_KICAD_CLI': cli,
                'ALX_KICAD_DESIGN_ROOTS': str(root / 'designs'),
            }, root / 'storage')
            checked = runtime.executors['check_kicad_board']({'project_dir': str(project)})
            self.assertEqual(checked.state, CapabilityResultState.SUCCEEDED, checked.failure)
            self.assertEqual(checked.values['totals']['total'], sum(
                group['count'] for key in ('violations', 'unconnected_items', 'schematic_parity')
                for group in checked.values[key]))
            # No optional edit: exercise real DRC and both SVG exports through production code.
            rendered = runtime.executors['apply_kicad_mechanical_fixes']({
                'work_copy_id': checked.values['work_copy_id']})
            self.assertEqual(rendered.state, CapabilityResultState.SUCCEEDED, rendered.failure)
            for value in (*rendered.values['before_images'], *rendered.values['after_images']):
                self.assertIn('<svg', Path(value).read_text())
            self.assertEqual({p.name: p.read_bytes() for p in project.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
