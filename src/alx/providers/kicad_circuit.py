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


def node(value):
    """A SPICE node name as the deck writes it, such as `/RF1` or `out-1`.

    Safe in a deck line, where tokens are separated by whitespace; never put
    into a control expression, where `/` and `-` are operators.
    """
    if not isinstance(value, str) or not re.fullmatch(r'[^\s;`$"\'(),=]+', value):
        raise AnalysisError('arguments_unusable')
    return value


def logical_lines(lines):
    """SPICE continuation lines (`+ ...`) joined to the line they continue."""
    joined = []
    for line in lines:
        if line.lstrip().startswith('+') and joined:
            joined[-1] = joined[-1] + ' ' + line.lstrip()[1:].strip()
        else:
            joined.append(line)
    return joined


def clean_deck(netlist, models):
    """Refuse executable control blocks, filesystem directives and hidden analyses.

    Model blocks are SPICE data, never ngspice command language. KiCad exports
    .save/.probe and analysis directives; we replace those with the requested run.
    """
    if not netlist or len(netlist) + sum(map(len, models)) > 1000000:
        raise AnalysisError('arguments_unusable')
    # Joined first, so a field on a continuation line, such as `+ AC 2`, is
    # seen and handled with the line it belongs to.
    lines = logical_lines(netlist.splitlines())
    result = [lines[0] if lines else 'ALX circuit']
    allowed = {'.model', '.subckt', '.ends', '.param', '.func', '.global', '.temp'}
    discarded = {'.end', '.ac', '.tran', '.op', '.save', '.probe', '.title'}
    for line in lines[1:] + [line for block in models for line in logical_lines(block.splitlines())]:
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


def measurement_expression(measurement, analysis, alias=lambda name: name):
    kind = measurement['kind']
    if kind in ('voltage', 'impedance'):
        a, b = alias(node(measurement['positive'])), alias(node(measurement['negative']))
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


# What one simulation may return to the Core: every row of every curve goes
# into its context, so a valid request must not be able to fill it.
MAX_POINTS = 2000
MAX_ROWS = 4000
MAX_CANDIDATES = 20
MAX_LOG_CHARS = 20000


def build_deck(lines, settings, measurements):
    kind = settings['kind']
    if kind == 'ac':
        start, stop = positive(settings['start_hz']), positive(settings['stop_hz'])
        count = settings['points']
        if not isinstance(count, int) or not 1 <= count <= MAX_POINTS or stop < start:
            raise AnalysisError('arguments_unusable')
        scale = settings.get('scale', 'dec')
        if scale not in ('dec', 'oct', 'lin'):
            raise AnalysisError('arguments_unusable')
        command = f'ac {scale} {count} {start:.17g} {stop:.17g}'
    elif kind == 'transient':
        step, stop = positive(settings['step_seconds']), positive(settings['stop_seconds'])
        if stop < step or stop / step > MAX_POINTS:
            raise AnalysisError('arguments_unusable')
        command = f'tran {step:.17g} {stop:.17g}'
    elif kind == 'operating_point':
        command = 'op'
    else:
        raise AnalysisError('arguments_unusable')
    if not 1 <= len(measurements) <= 32 or len({m['id'] for m in measurements}) != len(measurements):
        raise AnalysisError('arguments_unusable')
    if len(measurements) * (count if kind == 'ac' else stop / step if kind == 'transient' else 1) > MAX_ROWS:
        raise AnalysisError('arguments_unusable', {'reason': 'result_rows_exceed_bound',
                                                    'maximum_rows': MAX_ROWS})
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
        lines.append(f'Ialxprobe {node(m["negative"])} {node(m["positive"])} DC 0 AC 1')
    # A node whose name is not a plain identifier is measured through a unity
    # voltage-controlled source to a plain alias node, which draws no current.
    aliases = {}

    def alias(name):
        if name == '0' or re.fullmatch(r'[A-Za-z0-9_]+', name):
            return name
        if name not in aliases:
            aliases[name] = f'alxnode{len(aliases)}'
            lines.append(f'Ealxnode{len(aliases)-1} {aliases[name]} 0 {name} 0 1')
        return aliases[name]

    controls = ['.control', 'set noaskquit', 'set numdgt=15', 'set wr_vecnames', 'set wr_singlescale', command]
    for index, measurement in enumerate(measurements):
        expression = measurement_expression(measurement, kind, alias)
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
    tolerance_of = {}
    for ref, component in zip(refs, components):
        for item in [ref, *tied[ref]]:
            tolerance_of[item] = component['tolerance_fraction']
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
    # Nominal, and both perturbations of every part, tied parts included.
    if math.prod(len(c['values']) for c in components) * (1+2*len(tolerance_of)) > 1024:
        raise AnalysisError('arguments_unusable', {'reason': 'tuning_budget_exceeded'})
    candidates = []
    for combination in itertools.product(*(c['values'] for c in components)):
        values = dict(zip(refs, combination))
        together = {**values, **{item: values[ref] for ref in refs for item in tied[ref]}}
        nominal = solve(replace_values(lines, together))
        errors = target_errors(nominal['curves'], targets)
        sensitivity = []
        # Every part varies alone, tied parts too: each has its own tolerance.
        for ref, tolerance in tolerance_of.items():
            for sign in (-1, 1):
                varied = dict(together, **{ref: together[ref]*(1+sign*tolerance)})
                response = solve(replace_values(lines, varied))
                sensitivity.append({'reference': ref, 'fraction': sign*tolerance,
                                    'target_errors': target_errors(response['curves'], targets)})
        # Bounded: values, errors and sensitivity per candidate; the full
        # curves, deck and log of the run accompany the result only once.
        candidates.append({'values': together, 'target_errors': errors,
                           'score': sum(e['normalized_error']**2 for e in errors),
                           'within_targets': all(e['error'] == 0 for e in errors),
                           'sensitivity': sensitivity})
    candidates.sort(key=lambda c: c['score'])
    tie_margin = tuning['tie_margin']
    if not math.isfinite(tie_margin) or tie_margin < 0:
        raise AnalysisError('arguments_unusable')
    reasons = []
    if not any(c['within_targets'] for c in candidates):
        reasons.append('no_candidate_within_targets')
    if len(candidates) > 1 and candidates[1]['score']-candidates[0]['score'] <= tie_margin:
        reasons.append('equally_good_candidates')
    # The best few, ranked, and how many more were evaluated.
    return candidates[:MAX_CANDIDATES], reasons, max(0, len(candidates) - MAX_CANDIDATES)
