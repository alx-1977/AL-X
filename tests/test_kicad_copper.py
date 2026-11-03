"""Recorded FastHenry protocol and copper path evidence."""
import json
import math
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.config.settings import KiCadSettings
from alx.providers.kicad import KiCadProvider, KiCadError, inventory, sexpr
from alx.providers.coding_process import run_kicad_engine
from alx.providers.kicad_circuit import AnalysisError
from alx.providers.kicad_copper import (
    GeometryJudgement, arc_points, stackup, extract, geometry, fasthenry_input,
    parse_impedance, wheeler,
)


STACK = '''(setup (stackup
 (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (epsilon_r 4))
 (layer "F.Cu" (type "copper") (thickness 0.024))
 (layer "dielectric 1" (type "core") (thickness 0.065) (epsilon_r 4.1))
 (layer "B.Cu" (type "copper") (thickness 0.024))
 (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01) (epsilon_r 4))))'''
SEGMENT = '(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu"))'
OUTLINE = '(gr_rect (start -2 -2) (end 22 22) (layer "Edge.Cuts"))'
BOARD = '(kicad_pcb '+STACK+SEGMENT+OUTLINE+')'
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


    def test_pad_port_and_rotated_pad_position(self):
        pad = '''(footprint "test" (at 0 1 90) (property "Reference" "L1")
            (pad "1" smd rect (at 1 0 90) (size 1 0.5) (layers "F.Cu")))'''
        tree = sexpr(BOARD[:-1]+pad+')')
        layers = stackup(tree)
        port = dict(PORTS[0], start={'reference': 'L1', 'pad': '1'})
        # The pad at (1, 0) rotated 90 degrees about (0, 1) lands on (0, 0),
        # the track's start, so the port attaches there.
        (path,) = extract(tree, [port], 2, layers)
        self.assertEqual(path['edges'][0]['a'][:2], (0.0, 0.0))

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
        # A dead-end stub carries no current: the path is the coil alone.
        stubbed = BOARD[:-1]+'(segment (start 0 0) (end 0 5) (width 0.2) (layer "F.Cu")))'
        tree = sexpr(stubbed)
        (path,) = extract(tree, PORTS, 2, stackup(tree))
        self.assertEqual(len(path['edges']), 1)
        # A second route between the ends would share the current: refused.
        parallel = BOARD[:-1]+('(segment (start 0 0) (end 5 5) (width 0.2) (layer "F.Cu"))'
                               '(segment (start 5 5) (end 10 0) (width 0.2) (layer "F.Cu")))')
        tree = sexpr(parallel)
        with self.assertRaisesRegex(GeometryJudgement, 'parallel_route'):
            extract(tree, PORTS, 2, stackup(tree))
        pad = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
        board = BOARD[:-1]+'(footprint "test" (at 0 0) (property "Reference" "L1")'+pad+pad+'))'
        tree = sexpr(board)
        port = dict(PORTS[0], start={'reference': 'L1', 'pad': '1'})
        with self.assertRaisesRegex(GeometryJudgement, 'port_pad_ambiguous'):
            extract(tree, [port], 2, stackup(tree))

    def test_an_imported_two_layer_coil_traces_through_its_via_and_net_tie(self):
        """The Quad Coil's features: a via to B.Cu, a net tie from _P to _N, and
        track ends 15 um apart where the design joins them (2026-11-03)."""
        two_layer = STACK + (
            '(segment (start 0 0) (end 10 0) (width 0.4) (layer "F.Cu") (net "C_P"))'
            '(via (at 10 0) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "C_P"))'
            '(segment (start 10 0) (end 10 10) (width 0.4) (layer "B.Cu") (net "C_P"))'
            '(via (at 10 10) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "C_P"))'
            '(segment (start 10 10) (end 5.2 10) (width 0.4) (layer "F.Cu") (net "C_P"))'
            '(footprint "lib:NetTie" (at 5 10) (property "Reference" "NT1")'
            ' (pad "1" smd rect (at 0.2 0) (size 0.4 0.4) (layers "F.Cu") (net "C_P"))'
            ' (pad "2" smd rect (at -0.2 0) (size 0.4 0.4) (layers "F.Cu") (net "C_N")))'
            '(segment (start 4.8 10) (end 0.015 10) (width 0.4) (layer "F.Cu") (net "C_N"))'
            '(segment (start 0 10) (end 0 2) (width 0.4) (layer "F.Cu") (net "C_N"))')
        tree = sexpr('(kicad_pcb ' + two_layer + ')')
        layers = stackup(tree)
        port = {'id': 'c', 'start': {'x_mm': 0, 'y_mm': 0, 'layer': 'F.Cu'},
                'end': {'x_mm': 0, 'y_mm': 2, 'layer': 'F.Cu'}}
        (path,) = extract(tree, [port], 2, layers)
        kinds = [edge['kind'] for edge in path['edges']]
        self.assertEqual(kinds.count('via'), 2)
        self.assertEqual(kinds.count('net_tie'), 1)
        self.assertEqual(path['nets'], ['C_N', 'C_P'])
        deck = fasthenry_input([path], layers, 13.56e6, 2)
        self.assertIn('wx=1 wy=0 wz=0', deck)            # the vias' vertical bars

    def test_solver_grid_straightens_micrometre_jitter(self):
        """A track meant to be straight, 2.3 um off over 19 mm, is straight in the deck."""
        tree = sexpr('(kicad_pcb ' + STACK +
                     '(segment (start 0 0) (end 19 0.0023) (width 0.4) (layer "F.Cu")))')
        port = {'id': 't', 'start': {'x_mm': 0, 'y_mm': 0, 'layer': 'F.Cu'},
                'end': {'x_mm': 19, 'y_mm': 0.0023, 'layer': 'F.Cu'}}
        layers = stackup(tree)
        deck = fasthenry_input(extract(tree, [port], 2, layers), layers, 13.56e6, 2)
        ys = {line.split(' y=')[1].split()[0] for line in deck.splitlines() if line.startswith('N0_')}
        self.assertEqual(ys, {'0'})

    def test_a_tight_arc_is_never_cut_shorter_than_its_track_is_wide(self):
        # Review of #199: by true chord length, 2 r sin(step/2), not arc length.
        semicircle = sexpr('(arc (start 1 0) (mid 0 1) (end -1 0) (width 0.5))')
        points = arc_points(semicircle, 4, min_chord_mm=0.5)
        chords = [math.dist(a, b) for a, b in zip(points, points[1:])]
        self.assertGreaterEqual(min(chords), 0.5 - 1e-9)
        self.assertEqual(len(chords), 6)                  # not seven of 0.445 mm
        # Too tight for two chords of the width: one straight chord.
        tight = sexpr('(arc (start 0.15 0) (mid 0 0.15) (end -0.15 0) (width 0.4))')
        self.assertEqual(len(arc_points(tight, 4, min_chord_mm=0.4)), 2)

    def test_another_nets_track_crossing_the_middle_of_a_coil_is_a_short(self):
        """Review of #199: contact anywhere along a track, not only at its ends."""
        crossing = ('(segment (start 5 -3) (end 5 3) (width 0.2) (layer "F.Cu") (net "other"))')
        tree = sexpr(BOARD[:-1] + crossing + ')')
        with self.assertRaisesRegex(GeometryJudgement, 'off_path_copper_contact'):
            extract(tree, PORTS, 2, stackup(tree))

    def test_same_net_copper_touching_mid_track_is_an_unseen_route(self):
        touching = '(segment (start 5 0.05) (end 5 3) (width 0.2) (layer "F.Cu"))'
        tree = sexpr(BOARD[:-1] + touching + ')')
        with self.assertRaisesRegex(GeometryJudgement, 'off_path_copper_contact'):
            extract(tree, PORTS, 2, stackup(tree))

    def test_a_net_may_change_only_across_the_tie_that_joins_it(self):
        """Review of #199: one tie must not excuse a join to a third net."""
        def board(last_net):
            return sexpr('(kicad_pcb ' + STACK +
                '(segment (start 0 0) (end 4.8 0) (width 0.4) (layer "F.Cu") (net "A"))'
                '(footprint "lib:NetTie" (at 5 0) (property "Reference" "NT9")'
                ' (pad "1" smd rect (at -0.2 0) (size 0.4 0.4) (layers "F.Cu") (net "A"))'
                ' (pad "2" smd rect (at 0.2 0) (size 0.4 0.4) (layers "F.Cu") (net "B")))'
                '(segment (start 5.2 0) (end 8 0) (width 0.4) (layer "F.Cu") (net "B"))'
                f'(segment (start 8 0) (end 10 0) (width 0.4) (layer "F.Cu") (net "{last_net}")))')
        port = {'id': 'c', 'start': {'x_mm': 0, 'y_mm': 0, 'layer': 'F.Cu'},
                'end': {'x_mm': 10, 'y_mm': 0, 'layer': 'F.Cu'}}
        tree = board('B')
        (path,) = extract(tree, [port], 2, stackup(tree))
        self.assertEqual(path['nets'], ['A', 'B'])
        tree = board('C')
        with self.assertRaisesRegex(GeometryJudgement, 'port_crosses_nets:c:B to C'):
            extract(tree, [port], 2, stackup(tree))

    def test_a_through_via_joins_the_inner_layers_between_its_ends(self):
        """Review of #199: KiCad lists F.Cu and B.Cu, the via also meets In1.Cu."""
        four = ('(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035))'
                ' (layer "d1" (type "core") (thickness 0.2) (epsilon_r 4.5))'
                ' (layer "In1.Cu" (type "copper") (thickness 0.035))'
                ' (layer "d2" (type "core") (thickness 0.2) (epsilon_r 4.5))'
                ' (layer "B.Cu" (type "copper") (thickness 0.035))))')
        tree = sexpr('(kicad_pcb ' + four +
                     '(segment (start 0 0) (end 5 0) (width 0.3) (layer "F.Cu"))'
                     '(via (at 5 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu"))'
                     '(segment (start 5 0) (end 10 0) (width 0.3) (layer "In1.Cu")))')
        port = {'id': 'v', 'start': {'x_mm': 0, 'y_mm': 0, 'layer': 'F.Cu'},
                'end': {'x_mm': 10, 'y_mm': 0, 'layer': 'In1.Cu'}}
        (path,) = extract(tree, [port], 2, stackup(tree))
        self.assertIn('via', [edge['kind'] for edge in path['edges']])

    def test_a_track_ending_off_centre_inside_a_pad_attaches_to_it(self):
        pad = ('(footprint "test" (at -0.1 0) (property "Reference" "L1")'
               ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))')
        tree = sexpr(BOARD[:-1] + pad + ')')
        port = dict(PORTS[0], start={'reference': 'L1', 'pad': '1'})
        (path,) = extract(tree, [port], 2, stackup(tree))
        self.assertEqual(path['edges'][0]['a'][:2], (0.0, 0.0))

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
                          'mesh_densities': [2, 4], 'convergence_margin': .05, 'formula_margin': .1,
                          'self_capacitance_estimate_f': 3e-12}
        engine = root/'engine'
        engine.write_text('placeholder')
        engine.chmod(0o700)
        self.provider.engine_paths = {'fasthenry': engine}

    def test_analysis_snapshot_uses_system_temp_and_is_removed(self):
        initial = inventory(self.folder/'project')
        with self.provider._analysis_copy(self.copy_id) as (stage, meta):
            self.assertTrue(stage.is_relative_to(Path(tempfile.gettempdir())))
            self.assertFalse(stage.resolve().is_relative_to(Path(__file__).resolve().parents[1]))
            self.assertFalse(stage.resolve().is_relative_to(self.folder.resolve()))
            self.assertEqual((stage/'project'/meta['board']).read_text(), BOARD)
            (stage/'solver-output').write_text('disposable')
        self.assertFalse(stage.exists())
        self.assertEqual(inventory(self.folder/'project'), initial)

    def test_both_meshes_design_unchanged_and_stated_self_capacitance(self):
        initial = inventory(self.folder/'project')
        calls = []
        def runner(engine, arguments, directory):
            self.assertFalse(directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]))
            self.assertFalse(directory.resolve().is_relative_to(self.folder.resolve()))
            self.assertEqual(arguments, ['copper.inp'])
            calls.append(directory)
            (directory/'Zc.mat').write_text('Impedance matrix for frequency = 1.356e7 1 x 1\n .1 +10j\n')
            return SimpleNamespace(returncode=0, stdout='converged', stderr='')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        from alx.tools.kicad import DEFINITIONS
        definition = next(d for d in DEFINITIONS if d.capability_id == 'analyse_kicad_copper')
        self.assertTrue(definition.output_schema.accepts(result))
        self.assertEqual(len(calls), 2)                       # FastHenry only, once per mesh
        self.assertTrue(all(not directory.exists() for directory in calls))
        self.assertEqual(inventory(self.folder/'project'), initial)
        port = result['ports'][0]
        self.assertEqual(port['self_capacitance_f'], 3e-12)
        self.assertEqual(port['self_capacitance_source'], 'stated_estimate')
        self.assertAlmostEqual(port['self_resonant_frequency_hz'],
                               1/(2*math.pi*math.sqrt(port['inductance_h']*3e-12)))
        self.assertEqual(result['convergence']['inductance_h']['maximum_relative_difference'], 0)
        # A straight track is no square spiral: reported, not held as a doubt.
        self.assertEqual(result['formula_unavailable'][0]['port'], 'p')
        self.assertEqual(result['status'], 'facts')

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


    def test_installed_fasthenry_reproduces_a_published_square_loop(self):
        """Known reference: Grover's square loop, L = (2 mu0 a / pi)(ln(a/r) - 0.774).

        A 20 mm closed square loop of 0.2 mm x 24 um track, r the rectangular
        section's equivalent radius 0.2235 (w + t). FastHenry gave 82.6 and
        81.7 nH at densities 2 and 4 against 83.5 nH (2026-11-03).
        """
        engine = Path('~/.local/share/field-solvers/fasthenry-3.0wr/bin/fasthenry').expanduser()
        if not engine.is_file():
            self.skipTest('required engine binary genuinely absent: '+str(engine))
        side, gap = 20.0, 0.5
        corners = [(gap/2, 0), (side/2, 0), (side/2, side), (-side/2, side), (-side/2, 0), (-gap/2, 0)]
        tracks = ''.join(f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "F.Cu"))'
                         for a, b in zip(corners, corners[1:]))
        board = '(kicad_pcb (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.024))))'+tracks+')'
        (self.folder/'project/board.kicad_pcb').write_text(board)
        self.provider.engine_paths = {'fasthenry': engine}
        self.provider.execute_engine = run_kicad_engine
        port = {'id': 'loop', 'start': {'x_mm': gap/2, 'y_mm': 0, 'layer': 'F.Cu'},
                'end': {'x_mm': -gap/2, 'y_mm': 0, 'layer': 'F.Cu'}}
        result = self.provider.analyse_copper(dict(self.arguments, ports=[port]))
        mu0, a, r = 4*math.pi*1e-7, side*1e-3, 0.2235*(0.2e-3+24e-6)
        grover = 2*mu0*a/math.pi*(math.log(a/r)-0.774)
        self.assertLess(abs(result['ports'][0]['inductance_h']-grover)/grover, .05)
        self.assertLess(result['convergence']['inductance_h']['maximum_relative_difference'], .05)

    def test_installed_engines_on_pcbnew_square_spiral(self):
        settings = self.provider.settings
        engines = {
            'fasthenry': Path('~/.local/share/field-solvers/fasthenry-3.0wr/bin/fasthenry').expanduser(),
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
        source = source[:setup.start]+STACK+source[setup.end:]
        board.write_text(source[:source.rfind(')')]+OUTLINE+source[source.rfind(')'):])
        self.provider.engine_paths = engines
        self.provider.execute_engine = run_kicad_engine
        initial = inventory(self.folder/'project')
        ports = [dict(PORTS[0], end={'x_mm': 3, 'y_mm': 3, 'layer': 'F.Cu'})]
        started = time.monotonic()
        result = self.provider.analyse_copper(dict(self.arguments, ports=ports))
        self.assertLess(time.monotonic()-started, 120, 'both FastHenry meshes must finish within 120 s')
        self.assertEqual(len(result['meshes']), 2)
        self.assertFalse(any('nonconvergence' in reason for reason in result['reasons']), result['reasons'])
        self.assertLess(result['convergence']['inductance_h']['maximum_relative_difference'], .05)
        # This spiral is open: its ends are 4 mm apart with no return lead, so
        # FastHenry's partial inductance and the closed-coil formula differ
        # (about 25%). The comparison is reported for judgement; the known
        # reference is the closed loop below.
        self.assertEqual(len(result['formula']), 1)
        port = result['ports'][0]
        self.assertEqual(port['self_capacitance_source'], 'stated_estimate')
        self.assertGreater(port['self_resonant_frequency_hz'], 0)
        self.assertEqual(inventory(self.folder/'project'), initial)
