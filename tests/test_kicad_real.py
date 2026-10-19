"""Real CLI contract: only KiCad's upgraded output reaches the provider."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.contracts import CapabilityResultState

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
