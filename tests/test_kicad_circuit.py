"""SPICE protocol, bounded tuning, engine failures and design isolation."""
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.config.settings import KiCadSettings
from alx.providers.coding_process import run_kicad_engine
from alx.providers.kicad import KiCadProvider, KiCadError
from alx.providers.kicad_circuit import (
    AnalysisError, build_deck, clean_deck, parse_curve, enumerate_tuning,
    replace_values, target_errors,
)
from alx.tools.kicad import DEFINITIONS


class CircuitFormats(unittest.TestCase):
    def test_supplied_models_and_all_analysis_commands(self):
        lines = clean_deck('test\nV1 in 0 1\nD1 in 0 diode\n.end', ['.model diode D(Is=1e-14)'])
        measurements = [{'id': 'v', 'kind': 'voltage', 'positive': 'in', 'negative': '0'}]
        for settings, command in (({'kind': 'operating_point'}, '\nop\n'),
                ({'kind': 'transient', 'step_seconds': 1e-6, 'stop_seconds': .001}, '\ntran '),
                ({'kind': 'ac', 'start_hz': 1, 'stop_hz': 1e6, 'points': 10}, '\nac dec 10 ')):
            deck = build_deck(lines, settings, measurements)
            self.assertIn(command, deck)
            self.assertIn('.model diode D(Is=1e-14)', deck)
            self.assertEqual(deck.count('.end\n'), 1)

    def test_external_io_commands_are_rejected_in_decks_and_model_blocks(self):
        for directive in ('.control\nshell touch bad\n.endc', '.include /design',
                          '.lib /design', '.write /design', '.savebias /design',
                          '.load /design', '.options rawfile=/design'):
            for netlist, models in (('test\n'+directive, []), ('test\nR1 1 0 1', [directive])):
                with self.assertRaises(AnalysisError):
                    clean_deck(netlist, models)

    def test_impedance_has_explicit_excitation_and_zeros_other_ac_sources(self):
        lines = clean_deck('test\nV1 1 0 DC 3 AC 2 45\nR1 1 0 50', [])
        deck = build_deck(lines, {'kind': 'ac', 'start_hz': 1, 'stop_hz': 10, 'points': 2},
                          [{'id': 'z', 'kind': 'impedance', 'positive': '1', 'negative': '0'}])
        self.assertIn('V1 1 0 DC 3 AC 0', deck)
        self.assertIn('Ialxprobe 0 1 DC 0 AC 1', deck)
        self.assertIn('let alx0 = v(1)-0', deck)

    def test_recorded_wrdata_real_and_complex(self):
        self.assertEqual(parse_curve(' frequency alx0 alx0\n1.356000e7 50 -2.5\n', True),
                         [{'x': 13560000., 'real': 50., 'imag': -2.5}])
        self.assertEqual(parse_curve('time alx0\n0 1\n1e-6 2\n', False)[1]['real'], 2)
        for malformed in ('frequency alx0\n', '1 nan inf', '1 2'):
            with self.assertRaises(AnalysisError):
                parse_curve(malformed, True)

    def test_target_interpolation_and_missing_or_out_of_range_targets(self):
        curves = {'z': [{'x': 1., 'real': 40., 'imag': 10.}, {'x': 3., 'real': 60., 'imag': 20.}]}
        target = {'measurement_id': 'z', 'quantity': 'real', 'at': 2.,
                  'minimum': 49., 'maximum': 51., 'error_scale': 50.}
        self.assertEqual(target_errors(curves, [target])[0]['actual'], 50.)
        for changes in ({'at': 4.}, {'measurement_id': 'missing'}, {'error_scale': 0}):
            with self.assertRaises(AnalysisError):
                target_errors(curves, [dict(target, **changes)])

    def test_only_named_linear_values_change_and_duplicates_fail(self):
        lines = ['test', 'L1 a b 1e-6', 'R1 b 0 100', '.model keep D']
        changed = replace_values(lines, {'L1': 2e-6})
        self.assertEqual(changed[2:], lines[2:])
        self.assertEqual(lines[1], 'L1 a b 1e-6')
        for deck, values in ((lines, {'L9': 1e-6}), (lines+['L1 c d 1e-6'], {'L1': 1e-6}),
                             (['test', 'R1 a b {x} tc=1'], {'R1': 2})):
            with self.assertRaises(AnalysisError):
                replace_values(deck, values)

    def test_known_l_match_at_13_56mhz_with_tolerance_evidence(self):
        # A 200-ohm parallel load transforms to 50 ohms: Q=sqrt(3),
        # series reactance 86.60 ohms and shunt susceptance Q/200 S.
        frequency = 13.56e6
        omega = 2*math.pi*frequency
        optimal_l, optimal_c = math.sqrt(3)*50/omega, math.sqrt(3)/200/omega
        lines = ['L match', 'L1 in load 1e-6', 'C1 load 0 1e-10', 'R1 load 0 200']
        calls = []
        def solve(candidate):
            calls.append(candidate)
            values = {row.split()[0]: float(row.split()[3]) for row in candidate[1:]}
            impedance = 1j*omega*values['L1'] + 1/(1/values['R1']+1j*omega*values['C1'])
            return {'curves': {'z': [{'x': frequency, 'real': impedance.real, 'imag': impedance.imag}]},
                    'netlist': '\n'.join(candidate), 'log': 'analytical test oracle'}
        tuning = {'components': [
            {'reference': 'L1', 'values': [optimal_l, optimal_l*.8], 'tolerance_fraction': .01},
            {'reference': 'C1', 'values': [optimal_c, optimal_c*1.2], 'tolerance_fraction': .01}],
            'targets': [{'measurement_id': 'z', 'quantity': q, 'at': frequency,
                         'minimum': minimum, 'maximum': maximum, 'error_scale': 50}
                        for q, minimum, maximum in (('real', 49.9, 50.1), ('imag', -.1, .1))],
            'tie_margin': 1e-12}
        candidates, reasons, _ = enumerate_tuning(lines, tuning, solve)
        self.assertEqual(reasons, [])
        self.assertEqual(len(candidates), 4)
        self.assertEqual(len(calls), 20)
        self.assertEqual(candidates[0]['values'], {'L1': optimal_l, 'C1': optimal_c})
        self.assertTrue(candidates[0]['within_targets'])
        self.assertEqual(len(candidates[0]['sensitivity']), 4)
        self.assertNotIn('selected', candidates[0])
        tuning['targets'][0].update(minimum=1000, maximum=1001)
        _, reasons, _ = enumerate_tuning(lines, tuning, solve)
        self.assertIn('no_candidate_within_targets', reasons)
        tuning['tie_margin'] = 1e9
        _, reasons, _ = enumerate_tuning(lines, tuning, solve)
        self.assertIn('equally_good_candidates', reasons)


    def test_an_ac_source_on_a_continuation_line_is_zeroed_for_impedance(self):
        """Review of #199: `+ AC 2` belongs to the line it continues."""
        lines = clean_deck('test\nI1 0 in DC 0\n+ AC 2\nR1 in 0 50\n.end', [])
        deck = build_deck(lines, {'kind': 'ac', 'start_hz': 1e6, 'stop_hz': 2e6, 'points': 2},
                          [{'id': 'z', 'kind': 'impedance', 'positive': 'in', 'negative': '0'}])
        source = next(line for line in deck.splitlines() if line.startswith('I1'))
        self.assertIn('AC 0', source)
        self.assertNotIn('AC 2', deck)

    def test_kicad_node_names_are_measured_through_an_alias(self):
        """Review of #199: `/RF1` and `out-1` are valid nodes but operators in expressions."""
        lines = clean_deck('test\nV1 /RF1 0 AC 1\nR1 /RF1 out-1 50\nR2 out-1 0 50\n.end', [])
        deck = build_deck(lines, {'kind': 'ac', 'start_hz': 1e6, 'stop_hz': 2e6, 'points': 2},
                          [{'id': 'v', 'kind': 'voltage', 'positive': 'out-1', 'negative': '/RF1'}])
        self.assertIn('Ealxnode0 alxnode0 0 out-1 0 1', deck)
        self.assertIn('Ealxnode1 alxnode1 0 /RF1 0 1', deck)
        self.assertIn('let alx0 = v(alxnode0)-v(alxnode1)', deck)
        with self.assertRaises(AnalysisError):
            build_deck(lines, {'kind': 'ac', 'start_hz': 1e6, 'stop_hz': 2e6, 'points': 2},
                       [{'id': 'v', 'kind': 'voltage', 'positive': 'a(b)', 'negative': '0'}])

    def test_an_alias_never_reuses_a_name_the_deck_already_has(self):
        """Second review of #199: an existing alxnode0 must not be driven."""
        lines = clean_deck('test\nV1 /RF1 0 AC 1\nR1 /RF1 ALXNODE0 50\nR2 alxnode0 0 50\n.end', [])
        deck = build_deck(lines, {'kind': 'ac', 'start_hz': 1e6, 'stop_hz': 2e6, 'points': 2},
                          [{'id': 'v', 'kind': 'voltage', 'positive': '/RF1', 'negative': '0'}])
        self.assertIn('Ealxnode1 alxnode1 0 /RF1 0 1', deck)
        self.assertNotIn('Ealxnode0', deck)

    def test_a_result_too_large_for_the_core_is_refused_before_running(self):
        """Review of #199: every returned row enters the Core's context."""
        lines = clean_deck('test\nV1 a 0 AC 1\nR1 a 0 50\n.end', [])
        many = [{'id': f'v{i}', 'kind': 'voltage', 'positive': 'a', 'negative': '0'} for i in range(32)]
        with self.assertRaises(AnalysisError):
            build_deck(lines, {'kind': 'ac', 'start_hz': 1, 'stop_hz': 2, 'points': 1000}, many)
        with self.assertRaises(AnalysisError):
            build_deck(lines, {'kind': 'ac', 'start_hz': 1, 'stop_hz': 2, 'points': 10000}, many[:1])

    def test_tied_parts_each_vary_alone_and_only_the_best_candidates_return(self):
        """Review of #199: a tied part has its own tolerance run; results are bounded."""
        lines = clean_deck('test\nV1 a 0 AC 1\nC1 a b 1p\nC2 b 0 1p\nR1 b 0 50\n.end', [])
        calls = []

        def solve(candidate):
            calls.append(candidate)
            return {'curves': {'z': [{'x': 1e6, 'real': 50.0, 'imag': 0.0}]},
                    'netlist': '', 'log': ''}
        tuning = {'components': [
            {'reference': 'C1', 'values': [float(v)*1e-12 for v in range(1, 31)],
             'tolerance_fraction': .05, 'tied_references': ['C2']}],
            'targets': [{'measurement_id': 'z', 'quantity': 'real', 'at': 1e6,
                         'minimum': 40, 'maximum': 60, 'error_scale': 5}],
            'tie_margin': -1}
        with self.assertRaises(AnalysisError):          # tie_margin must not be negative
            enumerate_tuning(lines, tuning, solve)
        tuning['tie_margin'] = 0
        calls.clear()
        candidates, _, omitted = enumerate_tuning(lines, tuning, solve)
        self.assertEqual(len(calls), 30*5)               # nominal, C1 +-, C2 +-
        self.assertEqual({item['reference'] for item in candidates[0]['sensitivity']}, {'C1', 'C2'})
        self.assertEqual(candidates[0]['values']['C1'], candidates[0]['values']['C2'])
        self.assertEqual((len(candidates), omitted), (20, 10))
        self.assertNotIn('netlist', candidates[0])

class CircuitProvider(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = self.root / 'ngspice'
        self.engine.write_text('test engine placeholder')
        self.engine.chmod(0o700)
        self.provider = KiCadProvider(KiCadSettings(self.root/'cli', (self.root,), self.root/'work'), None)
        self.provider.engine_paths = {'ngspice': self.engine}
        self.arguments = {'netlist': 'resistor\nR1 in 0 50\n.end',
                          'analysis': {'kind': 'ac', 'start_hz': 1, 'stop_hz': 10, 'points': 2},
                          'measurements': [{'id': 'z', 'kind': 'impedance', 'positive': 'in', 'negative': '0'}]}

    def test_complete_netlist_log_and_temporary_isolation(self):
        directories = []
        def runner(engine, arguments, directory):
            directories.append(directory)
            self.assertEqual(arguments, ['-b', 'circuit.cir'])
            self.assertIn('Ialxprobe 0 in', (directory/'circuit.cir').read_text())
            (directory/'m0.txt').write_text('frequency alx0 alx0\n1 50 0\n10 50 0\n')
            return SimpleNamespace(returncode=0, stdout='full engine log', stderr='diagnostic')
        self.provider.execute_engine = runner
        result = self.provider.simulate_circuit(self.arguments)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('full engine log\ndiagnostic', result['log'])
        self.assertIn('.endc', result['netlist'])
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_missing_failed_and_incomplete_engines_fail(self):
        self.engine.unlink()
        with self.assertRaisesRegex(KiCadError, 'engine_unavailable'):
            self.provider.simulate_circuit(self.arguments)
        self.engine.write_text('engine')
        self.engine.chmod(0o700)
        for returncode in (1, 0):
            self.provider.execute_engine = lambda *args: SimpleNamespace(returncode=returncode, stdout='failure detail', stderr='')
            with self.assertRaises(KiCadError) as caught:
                self.provider.simulate_circuit(self.arguments)
            self.assertEqual(caught.exception.recovery['log'].strip(), 'failure detail')

    def test_exactly_two_added_actions_use_existing_runtime_and_permission(self):
        expected = {'check_kicad_board', 'edit_kicad_work_copy', 'export_kicad_manufacturing',
                    'read_reference_file', 'write_back_kicad_board', 'analyse_kicad_copper', 'simulate_kicad_circuit'}
        runtime = build_kicad_runtime(lambda: 'call', {}, self.root)
        self.assertEqual(set(runtime.executors), expected)
        for name in ('analyse_kicad_copper', 'simulate_kicad_circuit'):
            definition = next(d for d in DEFINITIONS if d.capability_id == name)
            self.assertEqual(definition.side_effect.value, 'none')
            self.assertFalse(runtime.policies[name].approval_required)

    def test_installed_engine_ac_transient_and_op(self):
        if not Path('/opt/homebrew/bin/ngspice').is_file():
            self.skipTest('ngspice binary absent')
        self.provider.engine_paths = {'ngspice': Path('/opt/homebrew/bin/ngspice')}
        self.provider.execute_engine = run_kicad_engine
        result = self.provider.simulate_circuit(self.arguments)
        self.assertAlmostEqual(result['curves']['z'][0]['real'], 50, places=4)
        for analysis in ({'kind': 'operating_point'},
                         {'kind': 'transient', 'step_seconds': 1e-6, 'stop_seconds': 1e-5}):
            arguments = dict(self.arguments, netlist='dc\nV1 in 0 DC 1\nR1 in 0 50\n.end',
                analysis=analysis, measurements=[{'id': 'v', 'kind': 'voltage', 'positive': 'in', 'negative': '0'}])
            result = self.provider.simulate_circuit(arguments)
            self.assertAlmostEqual(result['curves']['v'][0]['real'], 1, places=4)
