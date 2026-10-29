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
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
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
    def test_real_script_and_child_cannot_write_outside_copy(self):
        from alx.providers.coding_process import run_kicad_python
        from alx.config.settings import KiCadSettings
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            board = project / 'test.kicad_pcb'
            board.write_text('copy')
            original = root / 'original.kicad_pcb'
            original.write_text('original')
            script = root / 'edit.py'
            script.write_text(f'''
import os, sys, subprocess
from pathlib import Path
board = Path(sys.argv[1])
board.write_text('edited')
try:
    Path({str(original)!r}).write_text('escaped')
except PermissionError:
    pass
else:
    raise RuntimeError('write escaped')
child = subprocess.run([sys.executable, '-c', {('from pathlib import Path; Path(' + repr(str(original)) + ').write_text("escaped")')!r}], capture_output=True)
assert child.returncode != 0
print('confined')
''')
            settings = KiCadSettings.from_environment({}, root / 'storage')
            result = run_kicad_python(settings.kicad_python, script, board)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('confined', result.stdout)
            self.assertEqual(original.read_text(), 'original')
            self.assertEqual(board.read_text(), 'edited')

    def test_real_manufacturing_without_editing(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            root = Path(directory).resolve()
            project = root / 'designs/minimal'
            project.mkdir(parents=True)
            for suffix in ('.kicad_pcb', '.kicad_pro'):
                shutil.copy2(FIXTURE / ('minimal' + suffix), project)
            runtime = build_kicad_runtime(lambda: 'manufacturing', {
                'ALX_KICAD_CLI': CLI, 'ALX_KICAD_DESIGN_ROOTS': str(root / 'designs'),
            }, root / 'storage')
            checked = runtime.executors['check_kicad_board']({'project_dir': str(project)})
            self.assertEqual(checked.state, CapabilityResultState.SUCCEEDED, checked.failure)
            exported = runtime.executors['export_kicad_manufacturing']({
                'work_copy_id': checked.values['work_copy_id'], 'output_dir': 'production',
                'renames': {'F.Cu': 'top.gbr', 'F.Silkscreen': 'silk.gbr'}, 'zip_name': 'maker.zip'})
            self.assertEqual(exported.state, CapabilityResultState.SUCCEEDED, exported.failure)
            self.assertIn('X', (project / 'production/top.gbr').read_text())
            self.assertTrue(any(Path(f).suffix == '.drl' for f in exported.values['files_written']))

    def test_saved_project_drc_edit_and_manufacturing(self):
        cli = CLI
        fixture = FIXTURE
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
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
            edited = runtime.executors['edit_kicad_work_copy']({
                'work_copy_id': checked.values['work_copy_id'],
                'script': '''
import sys
import pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
enabled = board.GetEnabledLayers()
enabled.AddLayer(pcbnew.User_1)
board.SetEnabledLayers(enabled)
for item in board.GetDrawings():
    # GetLayerName() returns the display name (F.Silkscreen in this fixture).
    if item.GetLayer() == pcbnew.F_SilkS:
        item.SetLayer(pcbnew.User_1)
plot = board.GetPlotOptions()
selection = plot.GetLayerSelection()
selection.AddLayer(pcbnew.User_1)
plot.SetLayerSelection(selection)
board.SetPlotOptions(plot)
pcbnew.SaveBoard(sys.argv[1], board)
'''})
            self.assertEqual(edited.state, CapabilityResultState.SUCCEEDED, edited.failure)
            self.assertEqual(edited.values['change_report']['board']['layer_moves'], ({
                'id': 'd8478024-7b53-4e3c-b624-b00426b46aed', 'kind': 'gr_text',
                'before': ('F.SilkS',), 'after': ('User.1',)},))
            exported = runtime.executors['export_kicad_manufacturing']({
                'work_copy_id': checked.values['work_copy_id'], 'output_dir': 'production',
                'renames': {'User.1': 'pib_0.15.gbr'}, 'zip_name': 'production.zip'})
            self.assertEqual(exported.state, CapabilityResultState.SUCCEEDED, exported.failure)
            self.assertTrue((project / 'production/pib_0.15.gbr').is_file())
            self.assertEqual({p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}, before)


if __name__ == '__main__':
    unittest.main()
