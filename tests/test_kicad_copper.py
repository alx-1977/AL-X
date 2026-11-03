"""Recorded field-solver protocols and fail-closed copper geometry evidence."""
import json
import importlib.util
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
    copper_solids, electrostatic_files, winding_model, winding_capacitance,
    quad_panels, planar_panels,
)


requires_shapely = unittest.skipUnless(
    importlib.util.find_spec('shapely') is not None,
    'Shapely >=2.1 is required for copper surface meshing (requirements.txt)')


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
    @requires_shapely
    def test_spiral_surface_voltage_basis_refines_without_internal_cut_faces(self):
        x = y = 0
        segments = []
        for i in range(12):
            dx, dy = [(1, 0), (0, 1), (-1, 0), (0, -1)][i % 4]
            nx, ny = x+dx*(20-i//2), y+dy*(20-i//2)
            segments.append(f'(segment (start {x} {y}) (end {nx} {ny}) (width 0.2) (layer "F.Cu"))')
            x, y = nx, ny
        tree = sexpr('(kicad_pcb '+STACK+OUTLINE+''.join(segments)+')')
        layers = stackup(tree)
        ports = [dict(PORTS[0], end={'x_mm': x, 'y_mm': y, 'layer': 'F.Cu'})]
        from shapely.ops import unary_union
        for density in (2, 4):
            paths = extract(tree, ports, density, layers)
            solids, outline, nets = copper_solids(tree, paths, layers, density)
            patched, names, voltages = winding_model(paths[0], solids, density)
            self.assertEqual(len(names), 12*density)
            self.assertTrue(all(a < b for a, b in zip(voltages, voltages[1:])))
            self.assertAlmostEqual(unary_union([s for _, s, _, _ in patched]).area,
                                   unary_union([s for _, s, _, _ in solids]).area, delta=1e-16)
            files = electrostatic_files(patched, outline, names, layers, density, surface_patches=True)
            # Midpoint of the first straight edge is an internal voltage cut.
            # No vertical copper face may be added there.
            for name, source in files.items():
                if not name.startswith('conductor'):
                    continue
                for line in source.splitlines()[1:]:
                    pts = list(map(float, line.split()[2:]))
                    xs, zs = pts[0::3], pts[2::3]
                    self.assertFalse(all(abs(v-.01) < 1e-12 for v in xs) and max(zs) > min(zs))

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

    def test_fastcap_unit_headers_scale_every_matrix_entry_to_farads(self):
        for unit, factor in (('farads', 1.), ('millifarads', 1e-3), ('microfarads', 1e-6),
                             ('nanofarads', 1e-9), ('picofarads', 1e-12),
                             ('femtofarads', 1e-15), ('attofarads', 1e-18)):
            with self.subTest(unit=unit):
                matrix = parse_capacitance('CAPACITANCE MATRIX, '+unit+
                                           '\n 1 2\n1%GROUP19 1 2.5 -0.3\nother (GROUP20) 2 -0.3 3.1\n', 2)
                for actual, expected in zip(sum(matrix, []), (2.5, -.3, -.3, 3.1)):
                    self.assertTrue(math.isclose(actual, expected*factor, rel_tol=1e-14, abs_tol=0))

    def test_fastcap_requires_known_units_in_header_and_valid_matrix(self):
        for header in ('CAPACITANCE MATRIX', 'CAPACITANCE MATRIX,',
                       'CAPACITANCE MATRIX, coulombs', 'CAPACITANCE MATRIX, mysteryfarads'):
            with self.subTest(header=header), self.assertRaises(AnalysisError):
                parse_capacitance(header+'\na 1 2.5\n* picofarads in a comment\n', 1)
        with self.assertRaises(AnalysisError):
            parse_capacitance('CAPACITANCE MATRIX, picofarads\n1 nan', 1)

    def test_fastcap_unconverged_matrix_is_not_usable(self):
        for warning in ('gcr: WARNING exiting without converging',
                        'gmres: WARNING exiting without converging',
                        'NONCONVERGENCE AFTER 100 ITERATIONS'):
            with self.subTest(warning=warning), self.assertRaises(AnalysisError):
                parse_capacitance(warning+'\nCAPACITANCE MATRIX, attofarads\n1%GROUP19 1 1.688\n', 1)

    @requires_shapely
    def test_long_thin_panels_refine_along_length_without_rescaling_geometry(self):
        from shapely.geometry import Polygon
        face = [(0., 0., 0.), (.024, 0., 0.), (.024, 0., 10e-6), (0., 0., 10e-6)]
        for density in (2, 4):
            panels = list(quad_panels(face, density))
            area = 0.
            for a, b, c, d in panels:
                width, height = math.dist(a, b), math.dist(a, d)
                self.assertLessEqual(max(width/height, height/width), 8*(1+1e-10))
                area += width*height
            self.assertTrue(math.isclose(area, .024*10e-6, rel_tol=1e-10))
            narrow = Polygon([(0., 0.), (.02, 0.), (.02, .0002)])
            panels = list(planar_panels(narrow, 34e-6, density))
            self.assertTrue(all(p[2] == 34e-6 for panel in panels for p in panel))
            self.assertLessEqual(max(math.dist(a, b) for panel in panels
                                     for a, b in zip(panel, panel[1:]+panel[:1])),
                                 8*.0002/density*(1+1e-3))
            self.assertTrue(math.isclose(sum(Polygon([p[:2] for p in panel]).area for panel in panels),
                                         narrow.area, rel_tol=1e-12))

    @requires_shapely
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
        files, nets = fastcap_files(paths, layers, 2, tree)
        self.assertEqual(nets, ['0'])
        points = []
        for name, source in files.items():
            if name.startswith('conductor'):
                for line in source.splitlines()[1:]:
                    coords = list(map(float, line.split()[2:]))
                    points.extend(zip(coords[0::3], coords[1::3], coords[2::3]))
        # The emitted 10 mm trace is 0.01 m, 0.2 mm wide and 24 um thick.
        for axis, low, high in ((0, -.0001, .0101), (1, -.0001, .0001), (2, 10e-6, 34e-6)):
            self.assertTrue(math.isclose(min(p[axis] for p in points), low, rel_tol=1e-12))
            self.assertTrue(math.isclose(max(p[axis] for p in points), high, rel_tol=1e-12))
        self.assertIn('D ', files['copper.lst'])
        self.assertIn(' 4.1 ', files['copper.lst'])
        self.assertTrue(any(line.startswith('C ') and ' +' in line for line in files['copper.lst'].splitlines()))
        self.assertTrue(any(line.startswith('C ') and ' 4.1 ' in line for line in files['copper.lst'].splitlines()))

    @requires_shapely
    def test_dielectric_changes_deck_and_missing_outline_is_explicit(self):
        tree = sexpr(BOARD)
        layers = stackup(tree)
        paths = extract(tree, PORTS, 2, layers)
        files, _ = fastcap_files(paths, layers, 2, tree)
        changed = sexpr(BOARD.replace('(epsilon_r 4.1)', '(epsilon_r 2.2)').replace('(thickness 0.065)', '(thickness 0.13)'))
        other, _ = fastcap_files(paths, stackup(changed), 2, changed)
        self.assertNotEqual(files['copper.lst'], other['copper.lst'])
        self.assertNotEqual([v for k, v in files.items() if k.startswith('dielectric')],
                            [v for k, v in other.items() if k.startswith('dielectric')])
        with self.assertRaisesRegex(GeometryJudgement, 'board_outline_unresolved'):
            fastcap_files(paths, layers, 2, sexpr(BOARD.replace(OUTLINE, '')))

    @requires_shapely
    def test_pad_and_via_union_one_net_and_nominal_bore_barrel(self):
        board = BOARD.replace(SEGMENT, SEGMENT[:-1]+' (net 1))')[:-1]+'''
          (footprint "test" (at 0 0) (property "Reference" "L1")
            (pad "1" smd oval (at 0 0) (size 1 0.6) (layers "F.Cu") (net 1)))
          (via (at 10 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))'''
        tree = sexpr(board)
        layers = stackup(tree)
        solids, outline, nets = copper_solids(tree, [], layers, 2)
        self.assertEqual(nets, ['1'])
        files = electrostatic_files(solids, outline, nets, layers, 2)
        conductor_lines = [l for l in files['copper.lst'].splitlines() if l.startswith(('C ', 'B '))]
        self.assertTrue(any(l.startswith('B ') and ' 4.1 1 ' in l for l in conductor_lines))
        self.assertEqual(sum(not l.endswith('+') for l in conductor_lines), 1)
        from shapely.ops import unary_union
        front = [s for n, s, lo, hi in solids if n == '1' and lo < layers[1]['z_m'] < hi]
        union = unary_union(front)
        self.assertLess(union.area, sum(s.area for s in front))
        self.assertTrue(union.is_valid)
        # The drilled bore also removes the track that terminates at its centre.
        # There must be no horizontal electrode panel over the drill opening.
        from shapely.geometry import Polygon
        hole = next(s for n, s, _, _ in solids if n.startswith('@barrel:'))
        for name, source in files.items():
            if not name.startswith('conductor'):
                continue
            for line in source.splitlines()[1:]:
                coords = list(map(float, line.split()[2:]))
                zs = coords[2::3]
                if max(zs)-min(zs) < 1e-15:
                    face = Polygon(list(zip(coords[0::3], coords[1::3])))
                    self.assertLess(face.intersection(hole).area, 1e-16)

    @requires_shapely
    def test_through_hole_pad_uses_annuli_and_barrel_same_conductor(self):
        tree = sexpr(BOARD[:-1]+'''(footprint "test" (at 0 0) (property "Reference" "L1")
            (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4) (layers "*.Cu") )) )''')
        layers = stackup(tree)
        paths = extract(tree, PORTS, 2, layers)
        solids, outline, nets = copper_solids(tree, paths, layers, 2)
        self.assertEqual(nets, ['0'])
        self.assertEqual(sum(n.startswith('@barrel:') for n, _, _, _ in solids), 1)
        files = electrostatic_files(solids, outline, nets, layers, 2)
        lines = [l for l in files['copper.lst'].splitlines() if l.startswith(('C ', 'B '))]
        self.assertEqual(sum(not l.endswith('+') for l in lines), 1)
        with self.assertRaisesRegex(GeometryJudgement, 'multilayer'):
            winding_model(paths[0], solids, 2)

    def test_winding_energy_floating_common_mode_and_nonpositive(self):
        matrix = [[3e-12, -2e-12], [-2e-12, 3e-12]]
        c = winding_capacitance(matrix, [0., 1.])
        self.assertAlmostEqual(c, 2.5e-12, delta=1e-24)
        self.assertAlmostEqual(winding_capacitance(matrix, [7., 8.]), c, delta=1e-24)
        with self.assertRaisesRegex(GeometryJudgement, 'nonpositive'):
            winding_capacitance(matrix, [1., 1.])

    @requires_shapely
    def test_pad_port_and_rotated_pad_position(self):
        pad = '''(footprint "test" (at 0 1 90) (property "Reference" "L1")
            (pad "1" smd rect (at 1 0 90) (size 1 0.5) (layers "F.Cu")))'''
        tree = sexpr(BOARD[:-1]+pad+')')
        layers = stackup(tree)
        port = dict(PORTS[0], start={'reference': 'L1', 'pad': '1'})
        paths = extract(tree, [port], 2, layers)
        solids, _, _ = copper_solids(tree, paths, layers, 2)
        self.assertAlmostEqual(solids[-1][1].centroid.x, 0., delta=1e-12)
        self.assertAlmostEqual(solids[-1][1].centroid.y, 0., delta=1e-12)
        self.assertAlmostEqual(solids[-1][1].bounds[2]-solids[-1][1].bounds[0], .0005)

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

    @requires_shapely
    def test_both_engines_both_meshes_design_unchanged_and_winding_facts(self):
        initial = inventory(self.folder/'project')
        calls = []
        def runner(engine, arguments, directory):
            self.assertFalse(directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]))
            self.assertFalse(directory.resolve().is_relative_to(self.folder.resolve()))
            calls.append((arguments, directory))
            if arguments == ['copper.inp']:
                (directory/'Zc.mat').write_text('Impedance matrix for frequency = 1.356e7 1 x 1\n .1 +10j\n')
                return SimpleNamespace(returncode=0, stdout='converged', stderr='')
            self.assertEqual(arguments, ['-lcopper.lst'])
            size = sum(l.startswith(('C ', 'B ')) and not l.endswith(' +')
                       for l in (directory/'copper.lst').read_text().splitlines())
            matrix = '\n'.join(str(i+1)+' '+' '.join('2' if i == j else '-0.1' for j in range(size)) for i in range(size))
            return SimpleNamespace(returncode=0, stdout='CAPACITANCE MATRIX, picofarads\n'+matrix, stderr='')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        from alx.tools.kicad import DEFINITIONS
        definition = next(d for d in DEFINITIONS if d.capability_id == 'analyse_kicad_copper')
        self.assertTrue(definition.output_schema.accepts(result))
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({directory for _, directory in calls}), 4)
        self.assertTrue(all(not directory.exists() for _, directory in calls))
        self.assertEqual(inventory(self.folder/'project'), initial)
        self.assertEqual(result['status'], 'needs_judgement')
        port = result['ports'][0]
        self.assertGreater(port['self_capacitance_f'], 0)
        self.assertAlmostEqual(port['self_resonant_frequency_hz'],
                               1/(2*math.pi*math.sqrt(port['inductance_h']*port['self_capacitance_f'])))
        self.assertNotEqual(port['self_capacitance_f'], port['net_capacitance_to_infinity_f'])
        self.assertEqual(result['convergence']['inductance_h']['maximum_relative_difference'], 0)

    @requires_shapely
    def test_via_capacitance_remains_available_without_inventing_plating_for_henry(self):
        board = BOARD.replace(SEGMENT, SEGMENT[:-1]+' (net 1))')[:-1]+'''
          (via (at 10 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))'''
        (self.folder/'project/board.kicad_pcb').write_text(board)
        calls = []
        def runner(engine, arguments, directory):
            calls.append(arguments)
            self.assertEqual(arguments, ['-lcopper.lst'])
            return SimpleNamespace(returncode=0, stdout='CAPACITANCE MATRIX, picofarads\n1 2.0\n', stderr='')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['capacitance_nets'], ['1'])
        self.assertIn('via_barrel_plating_not_recorded', result['reasons'])
        self.assertIsNone(result['ports'][0]['inductance_h'])
        self.assertIsNone(result['ports'][0]['self_capacitance_f'])
        self.assertIsNone(result['ports'][0]['self_resonant_frequency_hz'])
        self.assertEqual(result['meshes'][1]['capacitance_f'], [[2e-12]])

    def test_missing_engine_and_tainted_copy_are_failures(self):
        self.provider.engine_paths = {}
        with self.assertRaisesRegex(KiCadError, 'engine_unavailable'):
            self.provider.analyse_copper(self.arguments)
        (self.folder/'metadata.json').write_text(json.dumps({'board': 'board.kicad_pcb', 'taint': 'pending'}))
        with self.assertRaisesRegex(KiCadError, 'work_copy_tainted'):
            self.provider.analyse_copper(self.arguments)

    @requires_shapely
    def test_solver_nonconvergence_returns_log_for_judgement(self):
        def runner(engine, arguments, directory):
            return SimpleNamespace(returncode=0, stdout='failed to converge', stderr='evidence')
        self.provider.execute_engine = runner
        result = self.provider.analyse_copper(self.arguments)
        self.assertIn('solver_nonconvergence', result['reasons'])
        self.assertIn('evidence', result['meshes'][0]['fasthenry_log'])

    @requires_shapely
    def test_fastcap_warning_with_matrix_is_reported_for_net_and_winding(self):
        for fail_winding in (False, True):
            def runner(engine, arguments, directory):
                if arguments == ['copper.inp']:
                    (directory/'Zc.mat').write_text('Impedance matrix for frequency = 1.356e7 1 x 1\n .1 +10j\n')
                    return SimpleNamespace(returncode=0, stdout='converged', stderr='')
                winding = directory.name.startswith('winding-')
                size = sum(l.startswith(('C ', 'B ')) and not l.endswith(' +')
                           for l in (directory/'copper.lst').read_text().splitlines())
                rows = '\n'.join(str(i+1)+' '+' '.join('2' if i == j else '-0.1' for j in range(size))
                                 for i in range(size))
                warning = 'gmres: WARNING exiting without converging\n' if winding == fail_winding else ''
                return SimpleNamespace(returncode=0, stdout='CAPACITANCE MATRIX, attofarads\n'+rows,
                                       stderr=warning)
            self.provider.execute_engine = runner
            result = self.provider.analyse_copper(self.arguments)
            with self.subTest(winding=fail_winding):
                self.assertEqual(result['status'], 'needs_judgement')
                if fail_winding:
                    self.assertIn('winding_solver_nonconvergence:p', result['reasons'])
                    self.assertEqual(result['meshes'][0]['self_capacitance_f'], [None])
                    self.assertIn('without converging', result['meshes'][0]['winding_logs'][0])
                else:
                    self.assertIn('solver_nonconvergence', result['reasons'])
                    self.assertNotIn('capacitance_f', result['meshes'][0])
                    self.assertIn('without converging', result['meshes'][0]['fastcap_log'])

    @requires_shapely
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
        source = source[:setup.start]+STACK+source[setup.end:]
        board.write_text(source[:source.rfind(')')]+OUTLINE+source[source.rfind(')'):])
        self.provider.engine_paths = engines
        self.provider.execute_engine = run_kicad_engine
        initial = inventory(self.folder/'project')
        ports = [dict(PORTS[0], end={'x_mm': 3, 'y_mm': 3, 'layer': 'F.Cu'})]
        result = self.provider.analyse_copper(dict(self.arguments, ports=ports))
        self.assertEqual(len(result['meshes']), 2)
        self.assertEqual(result['stackup'][2]['epsilon_r'], 4.1)
        self.assertTrue(all(len(m['self_capacitance_f']) == 1 for m in result['meshes']))
        self.assertEqual(len(result['formula']), 1)
        self.assertFalse(any('nonconvergence' in reason for reason in result['reasons']), result['reasons'])
        # There is no B.Cu ground plane in this fixture. Estimate winding
        # energy from adjacent, parallel turns, not from an invented plane
        # under the entire trace. Sidewalls give eps0*t*overlap/gap. The
        # two-wire logarithmic expression approximates coplanar strip fringe
        # capacitance; weight each pair by its squared arclength voltage drop.
        # The substrate is much thinner than the gap: interpolate between
        # vacuum and bulk epsilon by the fraction of gap occupied by the slab.
        eps0 = 8.8541878128e-12
        width, pitch = .2e-3, 1e-3
        gap = pitch-width
        thickness = result['stackup'][1]['thickness_m']
        substrate = result['stackup'][2]
        h, er = substrate['thickness_m'], substrate['epsilon_r']
        fringe_er = 1+(er-1)*2*h/(gap+2*h)
        per_length = eps0*(thickness/gap + math.pi*fringe_er/math.acosh(pitch/width))
        lengths = [(20-i//2)*1e-3 for i in range(12)]
        total = sum(lengths)
        midpoints = [(sum(lengths[:i])+length/2)/total for i, length in enumerate(lengths)]
        estimate = per_length*sum(min(lengths[i], lengths[i+4])*(midpoints[i+4]-midpoints[i])**2
                                  for i in range(8))
        # Allow a factor four for finite ends, nonadjacent turns, the thin
        # dielectric fringe approximation and piecewise-constant voltage mesh.
        # Check both meshes so a broken coarse solve cannot hide in the fine.
        for mesh in result['meshes']:
            capacitance = mesh['self_capacitance_f'][0]
            self.assertIsNotNone(capacitance, mesh.get('winding_logs'))
            self.assertGreater(capacitance, estimate/4)
            self.assertLess(capacitance, estimate*4)
        # Independently check the equipotential net against a disk of the
        # spiral's outer radius (C=8*eps0*r in vacuum), also in farads.
        infinity_estimate = 8*eps0*(20e-3/2)
        self.assertGreater(result['ports'][0]['net_capacitance_to_infinity_f'], infinity_estimate/4)
        self.assertLess(result['ports'][0]['net_capacitance_to_infinity_f'], infinity_estimate*4)
        self.assertGreater(result['ports'][0]['self_resonant_frequency_hz'], 0)
        self.assertLessEqual(result['formula'][0]['percentage_difference'], 10)
        self.assertEqual(inventory(self.folder/'project'), initial)

        # Hold copper geometry fixed and solve again in vacuum to verify that
        # the winding energy uses the saved stack-up dielectric interfaces.
        board.write_text(board.read_text().replace('(epsilon_r 4.1)', '(epsilon_r 1)')
                         .replace('(epsilon_r 4)', '(epsilon_r 1)'))
        vacuum = self.provider.analyse_copper(dict(self.arguments, ports=ports))
        self.assertGreater(result['ports'][0]['self_capacitance_f'],
                           vacuum['ports'][0]['self_capacitance_f'])
        self.assertLess(result['ports'][0]['self_resonant_frequency_hz'],
                        vacuum['ports'][0]['self_resonant_frequency_hz'])
