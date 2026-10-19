"""Real CLI contract: only KiCad's upgraded output reaches the provider."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.contracts import CapabilityResultState


@unittest.skipUnless(shutil.which('kicad-cli'), 'kicad-cli is not installed')
class RealKiCadTests(unittest.TestCase):
    def test_upgraded_project_drc_and_svg(self):
        cli = shutil.which('kicad-cli')
        fixture = Path(__file__).parent / 'fixtures' / 'kicad' / 'seed.kicad_pcb'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'designs' / 'minimal'
            project.mkdir(parents=True)
            board = project / 'minimal.kicad_pcb'
            # This is a hand-authored legacy seed, never presented as a saved fixture.
            # KiCad must successfully save its upgrade before provider testing begins.
            saved = subprocess.run([cli, 'pcb', 'upgrade', '--output', str(board), str(fixture)],
                                   capture_output=True, timeout=120, check=False)
            self.assertEqual(saved.returncode, 0, (saved.stdout, saved.stderr))
            self.assertTrue(board.is_file())
            self.assertNotEqual(board.read_bytes(), fixture.read_bytes())
            (project / 'minimal.kicad_pro').write_text('{}\n')
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
