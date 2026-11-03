"""Deterministic ngspice deck construction, measurements and discrete enumeration.

Only KiCadProvider owns the public simulation outcome. SI values throughout.
"""
from __future__ import annotations

import itertools
import math
import re


class AnalysisError(ValueError):
    def __init__(self, code, evidence=None):
        super().__init__(code)
        self.recovery = evidence or {}


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise AnalysisError('arguments_unusable')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_]+', value):
        raise AnalysisError('arguments_unusable')
    return value


def clean_deck(netlist, models):
    """Refuse executable control blocks, filesystem directives and hidden analyses.

    Model blocks are SPICE data, never ngspice command language. KiCad exports
    .save/.probe and analysis directives; we replace those with the requested run.
    """
    if not netlist or len(netlist) + sum(map(len, models)) > 1000000:
        raise AnalysisError('arguments_unusable')
    lines = netlist.splitlines()
    result = [lines[0] if lines else 'ALX circuit']
    allowed = {'.model', '.subckt', '.ends', '.param', '.func', '.global', '.temp'}
    discarded = {'.end', '.ac', '.tran', '.op', '.save', '.probe', '.title'}
    for line in lines[1:] + [line for block in models for line in block.splitlines()]:
        stripped = line.strip()
        if not stripped or stripped.startswith('*'):
            result.append(line)
            continue
        word = stripped.split()[0].lower()
        if word in discarded:
            continue
        if ((word.startswith('.') and word not in allowed)
                or any(token in stripped for token in (';', '`', '$', '"', "'"))):
            raise AnalysisError('arguments_unusable', {'reason': 'unsafe_or_unsupported_spice_directive', 'line': line})
        result.append(line)
    return result


def measurement_expression(measurement, analysis):
    kind = measurement['kind']
    if kind in ('voltage', 'impedance'):
        a, b = identifier(measurement['positive']), identifier(measurement['negative'])
        if kind == 'impedance' and analysis != 'ac':
            raise AnalysisError('arguments_unusable')
        # The generated AC current source injects +1 A at positive, returns at
        # negative. All user AC sources are zeroed for impedance runs.
        positive_voltage = '0' if a == '0' else f'v({a})'
        negative_voltage = '0' if b == '0' else f'v({b})'
        return f'{positive_voltage}-{negative_voltage}'
    if kind == 'current':
        return f'i({identifier(measurement["source"])})'
    raise AnalysisError('arguments_unusable')


def build_deck(lines, settings, measurements):
    kind = settings['kind']
    if kind == 'ac':
        start, stop = positive(settings['start_hz']), positive(settings['stop_hz'])
        count = settings['points']
        if not isinstance(count, int) or not 1 <= count <= 10000 or stop < start:
            raise AnalysisError('arguments_unusable')
        scale = settings.get('scale', 'dec')
        if scale not in ('dec', 'oct', 'lin'):
            raise AnalysisError('arguments_unusable')
        command = f'ac {scale} {count} {start:.17g} {stop:.17g}'
    elif kind == 'transient':
        step, stop = positive(settings['step_seconds']), positive(settings['stop_seconds'])
        if stop < step or stop / step > 100000:
            raise AnalysisError('arguments_unusable')
        command = f'tran {step:.17g} {stop:.17g}'
    elif kind == 'operating_point':
        command = 'op'
    else:
        raise AnalysisError('arguments_unusable')
    if not 1 <= len(measurements) <= 32 or len({m['id'] for m in measurements}) != len(measurements):
        raise AnalysisError('arguments_unusable')
    impedance = [m for m in measurements if m['kind'] == 'impedance']
    if impedance and (len(impedance) != 1 or len(measurements) != 1):
        raise AnalysisError('arguments_unusable', {'reason': 'impedance_requires_one_independent_excitation'})
    lines = list(lines)
    if impedance:
        for index, line in enumerate(lines[1:], 1):
            tokens = line.split()
            if tokens and tokens[0][0].lower() in ('v', 'i') and not line.lstrip().startswith('*'):
                # AC source fields are a SPICE format, not user-language routing.
                lines[index] = re.sub(r'\bac\s+[^\s]+(?:\s+[+-]?[0-9.]+)?', 'AC 0', line, flags=re.I)
        m = impedance[0]
        if any(line.split() and line.split()[0].lower() == 'ialxprobe' for line in lines):
            raise AnalysisError('arguments_unusable')
        lines.append(f'Ialxprobe {identifier(m["negative"])} {identifier(m["positive"])} DC 0 AC 1')
    controls = ['.control', 'set noaskquit', 'set numdgt=15', 'set wr_vecnames', 'set wr_singlescale', command]
    for index, measurement in enumerate(measurements):
        expression = measurement_expression(measurement, kind)
        controls.extend([f'let alx{index} = {expression}', f'wrdata m{index}.txt alx{index}'])
    return '\n'.join(lines + controls + ['quit', '.endc', '.end', ''])


def parse_curve(source, complex_values):
    rows = []
    for line in source.splitlines():
        try:
            values = [float(v) for v in line.split()]
        except ValueError:
            continue  # wr_vecnames header
        if len(values) != (3 if complex_values else 2) or not all(math.isfinite(v) for v in values):
            raise AnalysisError('invalid_report', {'reason': 'malformed_ngspice_curve'})
        rows.append({'x': values[0], 'real': values[1], 'imag': values[2] if complex_values else 0.0})
    if not rows:
        raise AnalysisError('invalid_report', {'reason': 'missing_ngspice_curve'})
    return rows


def replace_values(lines, values):
    result, found = [], set()
    for line in lines:
        tokens = line.split()
        ref = tokens[0].upper() if tokens else ''
        if ref in values:
            # Only linear two-terminal R/L/C values have one unambiguous slot.
            if ref in found or ref[0] not in 'RLC' or len(tokens) != 4:
                raise AnalysisError('arguments_unusable', {'reason': 'ambiguous_tunable_reference', 'reference': ref})
            tokens[3] = format(positive(values[ref]), '.17g')
            line = ' '.join(tokens)
            found.add(ref)
        result.append(line)
    if found != set(values):
        raise AnalysisError('arguments_unusable', {'reason': 'missing_tunable_reference'})
    return result


def target_errors(curves, targets):
    errors = []
    for target in targets:
        curve = curves.get(target['measurement_id'])
        if not curve:
            raise AnalysisError('arguments_unusable')
        x = target['at']
        # Interpolation is explicit; no silently selecting a nearby frequency.
        if not math.isfinite(x) or x < curve[0]['x'] or x > curve[-1]['x']:
            raise AnalysisError('arguments_unusable', {'reason': 'target_outside_analysis'})
        right = next((i for i, row in enumerate(curve) if row['x'] >= x), len(curve)-1)
        a, b = curve[max(0, right-1)], curve[right]
        fraction = (x-a['x'])/(b['x']-a['x']) if b['x'] != a['x'] else 0
        z = complex(a['real'], a['imag']) + fraction * complex(b['real']-a['real'], b['imag']-a['imag'])
        quantity = target['quantity']
        if quantity == 'q' and z.real <= 0:
            raise AnalysisError('invalid_report', {'reason': 'nonpositive_resistance_for_q'})
        actual = {'real': z.real, 'imag': z.imag, 'magnitude': abs(z),
                  'q': abs(z.imag)/z.real if z.real > 0 else 0}.get(quantity)
        low, high = target['minimum'], target['maximum']
        scale = positive(target['error_scale'])
        if actual is None or not all(math.isfinite(v) for v in (low, high)) or low > high:
            raise AnalysisError('arguments_unusable')
        error = max(low-actual, actual-high, 0)
        errors.append({'measurement_id': target['measurement_id'], 'quantity': quantity,
                       'at': x, 'actual': actual, 'minimum': low, 'maximum': high,
                       'error': error, 'normalized_error': error/scale})
    return errors


def enumerate_tuning(lines, tuning, solve):
    components, targets = tuning['components'], tuning['targets']
    if not components or not targets or len(components) > 8:
        raise AnalysisError('arguments_unusable')
    refs = [identifier(c['reference']).upper() for c in components]
    # Matched parts, such as the two halves of a balanced network, take the
    # tuned part's value together; each still varies alone for tolerance.
    tied = {ref: [identifier(item).upper() for item in c.get('tied_references', ())]
            for ref, c in zip(refs, components)}
    named = refs + [item for group in tied.values() for item in group]
    if len(set(named)) != len(named):
        raise AnalysisError('arguments_unusable')
    for component in components:
        values = component['values']
        if not values or len(set(values)) != len(values):
            raise AnalysisError('arguments_unusable')
        for value in values:
            positive(value)
        tolerance = component['tolerance_fraction']
        if not math.isfinite(tolerance) or not 0 < tolerance < 1:
            raise AnalysisError('arguments_unusable')
    # Includes nominal and both perturbations of every component. Bound before
    # launching any solve; never silently truncate the candidate space.
    if math.prod(len(c['values']) for c in components) * (1+2*len(components)) > 1024:
        raise AnalysisError('arguments_unusable', {'reason': 'tuning_budget_exceeded'})
    candidates = []
    for combination in itertools.product(*(c['values'] for c in components)):
        values = dict(zip(refs, combination))
        together = {**values, **{item: values[ref] for ref in refs for item in tied[ref]}}
        nominal = solve(replace_values(lines, together))
        errors = target_errors(nominal['curves'], targets)
        sensitivity = []
        for ref, component in zip(refs, components):
            for sign in (-1, 1):
                varied = dict(together, **{ref: values[ref]*(1+sign*component['tolerance_fraction'])})
                response = solve(replace_values(lines, varied))
                sensitivity.append({'reference': ref, 'fraction': sign*component['tolerance_fraction'],
                                    'target_errors': target_errors(response['curves'], targets),
                                    'netlist': response['netlist'], 'log': response['log']})
        candidates.append({'values': together, 'target_errors': errors,
                           'score': sum(e['normalized_error']**2 for e in errors),
                           'within_targets': all(e['error'] == 0 for e in errors),
                           'sensitivity': sensitivity, **nominal})
    candidates.sort(key=lambda c: c['score'])
    tie_margin = tuning['tie_margin']
    if not math.isfinite(tie_margin) or tie_margin < 0:
        raise AnalysisError('arguments_unusable')
    reasons = []
    if not any(c['within_targets'] for c in candidates):
        reasons.append('no_candidate_within_targets')
    if len(candidates) > 1 and candidates[1]['score']-candidates[0]['score'] <= tie_margin:
        reasons.append('equally_good_candidates')
    return candidates, reasons
