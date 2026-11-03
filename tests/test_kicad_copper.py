"""Recorded field-solver protocols and fail-closed copper geometry evidence."""
import json
import math
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.config.settings import KiCadSettings
from alx.providers.kicad import KiCadProvider, KiCadError, inventory, sexpr
from alx.providers.coding_process import run_kicad_engine
from alx.providers.kicad_circuit import AnalysisError
from alx.providers.kicad_copper import (
    GeometryJudgement, arc_points, stackup, extract, geometry, fasthenry_input,
    fastcap_files, parse_impedance, parse_capacitance, wheeler,
)


STACK = '''(setup (stackup
 (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (epsilon_r 4))
 (layer "F.Cu" (type "copper") (thickness 0.024))
 (layer "dielectric 1" (type "core") (thickness 0.065) (epsilon_r 4.1))
 (layer "B.Cu" (type "copper") (thickness 0.024))
 (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01) (epsilon_r 4))))'''
SEGMENT = '(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu"))'
BOARD = '(kicad_pcb '+STACK+SEGMENT+')'
PORTS = [{'id': 'p', 'start': {'x_mm': 0, 'y_mm': 0, 'layer': 'F.Cu'},
          'end': {'x_mm': 10, 'y_mm': 0, 'layer': 'F.Cu'}}]


class CopperFormats(unittest.TestCase):
    def test_recorded_fasthenry_complex_matrix_and_frequency_validation(self):
        output = '''Impedance matrix for frequency = 1.356e+07 2 x 2
 1.1e-1 +2.0e+1j  1e-3 -3.0e+0j
 1e-3 -3.0e+0j  2e-1 +4e+1j
'''
        matrix = parse_impedance(output, 2, 13.56e6)
        self.assertEqual(matrix, [[.11+20j, .001-3j], [.001-3j, .2+40j]])
        for size, frequency in ((1, 13.56e6), (2, 1e6)):
            with self.assertRaises(AnalysisError):
                parse_impedance(output, size, frequency)

    def test_recorded_fastcap_picofarad_units_not_farads(self):
        matrix = parse_capacitance('CAPACITANCE MATRIX, picofarads\n 1 2\n1 a 2.5 -0.3\n2 b -0.3 3.1\n', 2)
        self.assertAlmostEqual(matrix[0][0], 2.5e-12)
        self.assertAlmostEqual(matrix[0][1], -.3e-12)
        for output in ('CAPACITANCE MATRIX, farads\n1 2', 'CAPACITANCE MATRIX, picofarads\n1 nan'):
            with self.assertRaises(AnalysisError):
                parse_capacitance(output, 1)

    def test_board_stackup_and_exact_track_geometry(self):
        tree = sexpr(BOARD)
        layers = stackup(tree)
        self.assertAlmostEqual(layers[1]['thickness_m'], 24e-6)
        self.assertAlmostEqual(layers[2]['thickness_m'], 65e-6)
        self.assertEqual(layers[2]['epsilon_r'], 4.1)
        self.assertEqual(layers[0]['epsilon_r'], 4)
        paths = extract(tree, PORTS, 2, layers)
        facts = geometry(paths[0], layers)
        self.assertEqual(facts['layers'], ['F.Cu'])
        self.assertAlmostEqual(facts['total_length_m'], .01)
        self.assertEqual(facts['segment_count'], 1)
        deck = fasthenry_input(paths, layers, 13.56e6, 4)
        self.assertIn('nhinc=4 nwinc=4', deck)
        self.assertIn('sigma=5.8e7', deck)
        self.assertIn('w=0.0002 h=2.4e-05', deck)
        self.assertIn('.external N0_0 N0_1', deck)
        files = fastcap_files(paths, layers, 2)
        self.assertEqual(files['conductor0.qui'].count('\nQ '), 24)

    def test_arc_discretisation_refines_and_preserves_endpoints(self):
        arc = sexpr('(arc (start 1 0) (mid 0 1) (end -1 0))')
        coarse, fine = arc_points(arc, 2), arc_points(arc, 4)
        self.assertEqual(coarse[0], (1, 0))
        self.assertEqual(coarse[-1], (-1, 0))
        self.assertGreater(len(fine), len(coarse))
        length = sum(math.dist(a, b) for a, b in zip(fine, fine[1:]))
        self.assertAlmostEqual(length, math.pi, places=3)

    def test_missing_stackup_branching_and_duplicate_pads_return_judgement(self):
        with self.assertRaisesRegex(GeometryJudgement, 'stackup_missing'):
            stackup(sexpr('(kicad_pcb)'))
        branched = BOARD[:-1]+'(segment (start 0 0) (end 0 5) (width 0.2) (layer "F.Cu")))'
        tree = sexpr(branched)
        with self.assertRaisesRegex(GeometryJudgement, 'branch'):
            extract(tree, PORTS, 2, stackup(tree))
        pad = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
        board = BOARD[:-1]+'(footprint "test" (at 0 0) (property "Reference" "L1")'+pad+pad+'))'
        tree = sexpr(board)
        port = dict(PORTS[0], start={'reference': 'L1', 'pad': '1'})
        with self.assertRaisesRegex(GeometryJudgement, 'port_pad_ambiguous'):
            extract(tree, [port], 2, stackup(tree))

    def test_formula_refuses_a_nonspiral(self):
        tree = sexpr(BOARD)
        path = extract(tree, PORTS, 2, stackup(tree))[0]
        with self.assertRaises(GeometryJudgement):
            wheeler(path)


class CopperProvider(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.provider = KiCadProvider(KiCadSettings(root/'cli', (root,), root/'work'), None)
        self.copy_id = 'a'*32
        self.folder = self.provider.work_root/self.copy_id
        (self.folder/'project').mkdir(parents=True)
        (self.folder/'project/board.kicad_pcb').write_text(BOARD)
        (self.folder/'metadata.json').write_text(json.dumps({'board': 'board.kicad_pcb'}))
        self.arguments = {'work_copy_id': self.copy_id, 'ports': PORTS, 'frequency_hz': 13.56e6,
                          'mesh_densities': [2, 4], 'convergence_margin': .05, 'formula_margin': .1}
        engine = root/'engine'
        engine.write_text('placeholder')
        engine.chmod(0o700)
        self.provider.engine_paths = {'fasthenry': engine, 'fastcap': engine}

    def test_both_engines_both_meshes_design_unchanged_and_no_false_selfcap(self):
        initial = inventory(self.folder/'project')
        calls = []
        def runner(engine, arguments, directory):
            calls.append((arguments, directory))
            if arguments == ['copper.inp']:
                (directory/'Zc.mat').write_text('Impedance matrix for frequency = 1.356e7 1 x 1\n .1 +10j\n')
                return SimpleNamespace(returncode=0, stdout='converged', stderr='')
            return SimpleNamespace(returncode=0, stdout='CAPACITANCE MATRIX, picofarads\n1 2.0\n', stderr='')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({directory for _, directory in calls}), 2)
        self.assertTrue(all(not directory.exists() for _, directory in calls))
        self.assertEqual(inventory(self.folder/'project'), initial)
        self.assertEqual(result['status'], 'needs_judgement')
        self.assertIsNone(result['ports'][0]['self_capacitance_f'])
        self.assertIsNone(result['ports'][0]['self_resonant_frequency_hz'])
        self.assertIn('distributed_self_capacitance_and_dielectric_mesh_unresolved', result['reasons'])
        self.assertEqual(result['convergence']['inductance_h']['maximum_relative_difference'], 0)

    def test_missing_engine_and_tainted_copy_are_failures(self):
        self.provider.engine_paths = {}
        with self.assertRaisesRegex(KiCadError, 'engine_unavailable'):
            self.provider.analyse_copper(self.arguments)
        (self.folder/'metadata.json').write_text(json.dumps({'board': 'board.kicad_pcb', 'taint': 'pending'}))
        with self.assertRaisesRegex(KiCadError, 'work_copy_tainted'):
            self.provider.analyse_copper(self.arguments)

    def test_solver_nonconvergence_returns_log_for_judgement(self):
        def runner(engine, arguments, directory):
            return SimpleNamespace(returncode=0, stdout='failed to converge', stderr='evidence')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        self.assertIn('solver_nonconvergence', result['reasons'])
        self.assertIn('evidence', result['meshes'][0]['fasthenry_log'])

    def test_installed_engines_on_pcbnew_square_spiral(self):
        settings = self.provider.settings
        engines = {
            'fasthenry': Path('~/.local/share/field-solvers/fasthenry-3.0wr/bin/fasthenry').expanduser(),
            'fastcap': Path('~/.local/share/field-solvers/fastcap-2.0wr/bin/fastcap').expanduser(),
        }
        for path in [settings.kicad_python, *engines.values()]:
            if not path.is_file():
                self.skipTest('required engine binary genuinely absent: '+str(path))
        board = self.folder/'project/board.kicad_pcb'
        script = self.folder/'make_spiral.py'
        script.write_text('''import pcbnew, sys
b = pcbnew.BOARD()
x = y = 0
directions = [(1,0),(0,1),(-1,0),(0,-1)]
for i in range(12):
    length = 20-i//2
    dx,dy = directions[i%4]
    nx,ny = x+dx*length,y+dy*length
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(y)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(nx),pcbnew.FromMM(ny)))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    b.Add(t)
    x,y = nx,ny
pcbnew.SaveBoard(sys.argv[1],b)
''')
        process = subprocess.run([str(settings.kicad_python), str(script), str(board)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        source = board.read_text()
        setup = sexpr(source).one('setup')
        board.write_text(source[:setup.start]+STACK+source[setup.end:])
        self.provider.engine_paths = engines
        self.provider.execute_engine = run_kicad_engine
        initial = inventory(self.folder/'project')
        ports = [dict(PORTS[0], end={'x_mm': 3, 'y_mm': 3, 'layer': 'F.Cu'})]
        result = self.provider.analyse_copper(dict(self.arguments, ports=ports))
        self.assertEqual(len(result['meshes']), 2)
        self.assertEqual(len(result['formula']), 1)
        self.assertLessEqual(result['formula'][0]['percentage_difference'], 10)
        self.assertEqual(inventory(self.folder/'project'), initial)
