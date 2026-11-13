"""Bounded KiCad mechanics (D-061, amending D-058); never interprets user language."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import zipfile
import difflib
import unicodedata
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager, suppress
import fcntl

from alx.providers.credential_paths import denied
from alx.providers.kicad_helpers import HELPERS, script_runner
from alx.providers.kicad_rules import check_layout_rules
from alx.providers.reference_file import reference_path, read_reference, archive_entries, read_bytes, credential_path
from alx.providers.coding_process import KICAD_CLI_UNAVAILABLE_ERRORS, KICAD_CLI_TIMEOUT
from alx.providers.kicad_circuit import (
    AnalysisError, clean_deck, build_deck, parse_curve, enumerate_tuning,
)
from alx.providers.kicad_copper import (
    SOLVER_GRID_M, GeometryJudgement, stackup, extract, geometry, fasthenry_input,
    solver_nonconverged, parse_impedance, wheeler,
)

logger = logging.getLogger(__name__)



# What one check result carries per DRC check type (D-057a).
FINDINGS_PER_CHECK_TYPE = 8
NOISE_EXAMPLES = 2
ITEMS_PER_FINDING = 4

MAX_PROJECT_ENTRIES = 20000
MAX_PROJECT_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
# Retained edit evidence has its own limit, outside the project limits.
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024 * 1024


class KiCadError(Exception):
    def __init__(self, code, recovery=None):
        super().__init__(code)
        self.recovery = recovery


def bounded(path, roots, *, protected=True):
    raw = Path(path).expanduser().absolute()
    resolved = raw.resolve()
    if ((protected and (denied(raw.as_posix()) or denied(resolved.as_posix())))
            or not any(resolved.is_relative_to(root) for root in roots)):
        raise KiCadError('path_denied')
    return resolved


def inventory(root, *, exclude=frozenset()):
    exclude = frozenset(exclude)
    result = {}
    total = 0
    entries = 0

    def walk():
        # Excluded paths are pruned before they are entered, so an excluded
        # tree costs nothing and cannot hide work from the entry limit
        # (review of #208). Only exact paths, never a filename pattern.
        for directory, folders, files in os.walk(root):
            here = Path(directory)
            folders[:] = [name for name in folders if here / name not in exclude]
            for name in (*folders, *files):
                if here / name not in exclude:
                    yield here / name

    for path in walk():
        entries += 1
        if entries > MAX_PROJECT_ENTRIES:
            raise KiCadError('project_too_large')
        bounded(path, (root,), protected=False)
        if credential_path(path.relative_to(root).as_posix()):
            raise KiCadError('path_denied')
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise KiCadError('path_denied')
        if path.is_file():
            total += path.stat().st_size
            if path.stat().st_size > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES:
                raise KiCadError('project_too_large')
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


# KiCad keeps its own automatic save history in this folder at the top of a
# project, as a git repository. It is KiCad's history, not the design, and its
# `.git` made every saved project look like a credential path: on 2026-11-05
# Friedl's Quad Coil project was refused outright. Exactly this one folder at
# the top of a design is skipped; the credential check stays everywhere else.
KICAD_LOCAL_HISTORY = '.history'


def not_design(source):
    """What KiCad keeps beside a design that is not the design, by exact path.

    Its save history, its backup folder (named `backups` in Friedl's layout,
    `<project>-backups` by default) and each project's local settings file,
    which KiCad rewrites on every open and close. On 2026-11-12 the backups
    were 31 of the 48 MB copied into every work copy and its evidence, and a
    rewritten settings file twice refused a write-back as a changed board.
    """
    stems = sorted(path.stem for path in source.glob('*.kicad_pro'))
    names = {KICAD_LOCAL_HISTORY, 'backups', *(stem + '-backups' for stem in stems),
             *(stem + '.kicad_prl' for stem in stems)}
    return {source / name for name in names}


DESIGN_SUFFIXES = frozenset({'.kicad_pcb', '.kicad_pro', '.kicad_dru', '.kicad_sch', '.kicad_sym',
                             '.kicad_mod', '.kicad_wks'})
DESIGN_NAMES = frozenset({'fp-lib-table', 'sym-lib-table'})


def is_design_file(name):
    """A file KiCad keeps as part of a design: boards, schematics, libraries, rules."""
    path = Path(name)
    return path.suffix in DESIGN_SUFFIXES or path.name in DESIGN_NAMES


def top_level_sheets(project):
    """How many top-level sheets the project's schematic has (KiCad 10 project file).

    An Altium import makes one per Altium sheet. KiCad's command-line check
    of the board against its schematic reads only the first of them
    (2026-11-13: an AE2 pin swap went unseen); make_hierarchical joins them.
    """
    try:
        settings = json.loads(next(iter(sorted(project.glob('*.kicad_pro')))).read_text())
    except (StopIteration, OSError, ValueError):
        return 0
    return len((settings.get('schematic') or {}).get('top_level_sheets') or ()) or 1


def script_hint(stderr):
    """What a common edit-script failure means, so she need not work it out (2026-11-13)."""
    if any(mark in stderr for mark in ('WriteDRCReport', 'fork_exec', 'Operation not permitted')):
        return ('A script cannot run DRC or start programs. Every edit report already gives '
                'DRC totals; for the findings, use action check on this copy.')
    if "has no attribute" in stderr:
        return ("That pcbnew name does not exist in KiCad 10. Use the helpers (fields and "
                "set_field for part fields), or print dir() of the object before guessing.")
    return ''


def design_inventory(source, *, exclude=frozenset()):
    """A design folder's inventory without what KiCad keeps beside the design."""
    return inventory(source, exclude={*exclude, *not_design(source)})


_LIBRARY_SKIPPED = frozenset({'.git', '__pycache__', 'cache', 'node_modules'})


def library_inventory(source, *, exclude=frozenset()):
    """A library folder's KiCad files only: footprints, symbols, library tables.

    The FireFli library also holds a 749 MB download cache, scripts and its
    git history (2026-11-13); none of it is a part, and a commit or a cache
    refresh must never read as a changed original.
    """
    exclude = frozenset(exclude)
    result, total = {}, 0
    for directory, folders, files in os.walk(source):
        here = Path(directory)
        folders[:] = sorted(name for name in folders if name not in _LIBRARY_SKIPPED
                            and here / name not in exclude and not (here / name).is_symlink())
        for name in sorted(files):
            path = here / name
            if path in exclude or path.is_symlink() or not is_design_file(name):
                continue
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES:
                raise KiCadError('project_too_large')
            result[path.relative_to(source).as_posix()] = sha(path)
    return result


def _library_files(source):
    """A copytree ignore keeping only a library's KiCad files and their folders."""
    def ignore(directory, names):
        here = Path(directory)
        return [name for name in names if name in _LIBRARY_SKIPPED or (
            not (here / name).is_dir() and not is_design_file(name))]
    return ignore


def _without_local_history(source):
    """A copytree ignore that leaves out exactly what is not the design."""
    skipped = {path.name for path in not_design(source)}

    def ignore(directory, names):
        return [name for name in names if name in skipped] if Path(directory) == source else []
    return ignore


def evidence_bytes(folder):
    """Size of retained edit evidence; refuses anything but plain files and folders."""
    total = 0
    if folder.is_symlink():
        raise KiCadError('path_denied')
    for path in folder.rglob('*') if folder.exists() else ():
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise KiCadError('path_denied')
        if path.is_file():
            total += path.stat().st_size
    return total


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Node:
    def __init__(self, start, end, children):
        self.start, self.end, self.children = start, end, children

    @property
    def name(self):
        return self.children[0] if self.children else ''

    def named(self, name):
        return [x for x in self.children if isinstance(x, Node) and x.name == name]

    def one(self, name):
        values = self.named(name)
        if len(values) != 1:
            raise KiCadError('unsupported_project')
        return values[0]


def sexpr(board_source):
    """Parse spans; edits preserve unrelated KiCad bytes."""
    stack, roots = [], []
    i = 0
    while i < len(board_source):
        c = board_source[i]
        if c.isspace():
            i += 1
        elif c == '#':
            newline = board_source.find('\n', i)
            i = len(board_source) if newline < 0 else newline + 1
        elif c == '(':
            node = Node(i, 0, [])
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
            i += 1
        elif c == ')':
            if not stack:
                raise KiCadError('unsupported_project')
            stack.pop().end = i + 1
            i += 1
        else:
            start = i
            if c == '"':
                i += 1
                while i < len(board_source) and board_source[i] != '"':
                    i += 2 if board_source[i] == '\\' else 1
                i += 1
                try:
                    value = json.loads(board_source[start:i])
                except ValueError as error:
                    raise KiCadError('unsupported_project') from error
            else:
                while i < len(board_source) and not board_source[i].isspace() and board_source[i] not in '()':
                    i += 1
                value = board_source[start:i]
            if not stack:
                raise KiCadError('unsupported_project')
            stack[-1].children.append(value)
    if stack or len(roots) != 1:
        raise KiCadError('unsupported_project')
    return roots[0]


def descendants(node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from descendants(child)


def references(footprint):
    return ([p.children[2] for p in footprint.named('property') if p.children[1] == 'Reference']
            + [p.children[2] for p in footprint.named('fp_text') if p.children[1] == 'reference'])


def board_facts(source):
    tree = sexpr(source)
    items, counts = {}, Counter()
    for index, node in enumerate(descendants(tree)):
        layers = node.named('layer') + node.named('layers')
        if not layers or node.name in ('setup', 'stackup', 'kicad_pcb'):
            continue
        layer = [str(v) for v in layers[0].children[1:] if isinstance(v, str)]
        uid = node.named('uuid') + node.named('tstamp')
        identity = str(uid[0].children[1]) if uid else f'{node.name}:{index}'
        items[identity] = {'id': identity, 'kind': node.name, 'layers': layer}
        counts.update(layer)
    def section(name):
        return '\n'.join(source[n.start:n.end] for n in tree.named(name))
    return {'items': items, 'counts': dict(counts), 'layers': section('layers'),
            'setup': section('setup'),
            'stackup': '\n'.join(source[n.start:n.end] for n in descendants(tree) if n.name == 'stackup')}


def structural_diff(before, after):
    left, right = before['items'], after['items']
    added = [right[k] for k in right.keys() - left.keys()]
    removed = [left[k] for k in left.keys() - right.keys()]
    moved = [{'id': k, 'kind': right[k]['kind'], 'before': left[k]['layers'],
              'after': right[k]['layers']} for k in sorted(left.keys() & right.keys())
             if left[k]['layers'] != right[k]['layers']]
    sections = {key: {'before': before[key][:6000], 'after': after[key][:6000],
                      'changed': before[key] != after[key],
                      'truncated': max(len(before[key]), len(after[key])) > 6000}
                for key in ('layers', 'setup', 'stackup')}
    return dict(sections, items_added=sorted(added, key=lambda x: x['id'])[:100],
                items_removed=sorted(removed, key=lambda x: x['id'])[:100], layer_moves=moved[:100],
                added_count=len(added), removed_count=len(removed), moved_count=len(moved),
                truncated=max(len(added), len(removed), len(moved)) > 100,
                per_layer_before=before['counts'], per_layer_after=after['counts'])


def exact_filename(value):
    if (not value or Path(value).name != value or value in ('.', '..') or '\\' in value
            or len(value) > 240 or any(ord(c) < 32 for c in value) or denied(value)):
        raise KiCadError('path_denied')
    return value


def normalized_manufacturing(data):
    gerber_source = data.decode('utf-8', errors='replace')
    if gerber_source.lstrip().startswith('{'):
        try:
            value = json.loads(gerber_source)
            if isinstance(value.get('Header'), dict):
                for key in ('CreationDate', 'GenerationSoftware'):
                    value['Header'].pop(key, None)
            return json.dumps(value, sort_keys=True, indent=2).splitlines()
        except (ValueError, AttributeError):
            pass
    # Remove only complete metadata records, preserving geometry sharing a line.
    gerber_source = re.sub(r'G04[^*]*\*', '', gerber_source)
    # SameCoordinates only tags files that share an origin; geometry is compared below.
    gerber_source = re.sub(r'%TF\.(?:CreationDate|GenerationSoftware|SameCoordinates),[^*]*\*%', '', gerber_source)
    lines = gerber_source.splitlines()
    return [line.rstrip() for line in lines if not (
        not line.strip()
        or line.lstrip().startswith(('; #@! TF.CreationDate,', '; #@! TF.GenerationSoftware,',
                                     '; Creation date:', '; GenerationSoftware:', '; DRILL file')))]


def compare_manufacturing(files, previous):
    path = reference_path(previous)
    if path.is_dir():
        names = inventory(path)
        old = {name: read_bytes(path / name) for name in names}
    else:
        import io
        with zipfile.ZipFile(io.BytesIO(read_bytes(path))) as bundle:
            entries = archive_entries(bundle)
            old = {e.filename: bundle.read(e) for e in entries if not e.is_dir()}
    result = []
    for name in sorted(files.keys() | old.keys()):
        count = 0
        if name not in old:
            status = 'only_in_new'
        elif name not in files:
            status = 'only_in_previous'
        else:
            left, right = normalized_manufacturing(old[name]), normalized_manufacturing(files[name])
            status = 'identical' if left == right else 'differs'
            if status == 'differs':
                # Linear multiset difference is bounded and counts repeated geometry.
                count = sum((Counter(left) - Counter(right)).values()) + sum((Counter(right) - Counter(left)).values())
                if count == 0:  # Ordering changes can matter to Gerber modal state.
                    count = sum(a != b for a, b in zip(left, right))
        result.append({'name': name, 'status': status, 'differing_lines': min(count, 10000),
                       'line_count_truncated': count > 10000})
    return {'files': result[:200], 'truncated': len(result) > 200}


class KiCadProvider:
    def __init__(self, settings, execute_cli, execute_python=None):
        self.settings, self.execute_cli = settings, execute_cli
        self.execute_python = execute_python
        self.design_roots = tuple(p.expanduser().resolve() for p in settings.design_roots)
        self.work_root = settings.work_root.expanduser().resolve()
        self.engine_paths = {}
        self.execute_engine = None
        self.execute_page = None

    def _engine(self, name, arguments, directory):
        executable = self.engine_paths.get(name)
        try:
            available = executable is not None and executable.is_file() and os.access(executable, os.X_OK)
        except OSError as error:
            raise KiCadError('engine_unavailable', {'engine': name, 'path': str(executable),
                                                   'error': str(error)}) from error
        if not available or self.execute_engine is None:
            raise KiCadError('engine_unavailable', {'engine': name, 'path': str(executable)})
        try:
            result = self.execute_engine(executable, arguments, directory)
        except KICAD_CLI_TIMEOUT as error:
            raise KiCadError('engine_timeout', {'engine': name}) from error
        except OSError as error:
            raise KiCadError('engine_unavailable', {'engine': name, 'error': str(error)}) from error
        log = (getattr(result, 'stdout', '') or '') + '\n' + (getattr(result, 'stderr', '') or '')
        if result.returncode:
            raise KiCadError('engine_failed', {'engine': name, 'returncode': result.returncode, 'log': log})
        return log

    @contextmanager
    def _analysis_copy(self, work_copy_id):
        """Analyse disposable snapshots; neither engine ever receives a design path."""
        with self._locked(work_copy_id):
            root, meta = self._load(work_copy_id)
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            original = inventory(root / 'project')
            with tempfile.TemporaryDirectory(prefix='alx-analysis-') as temporary:
                stage = Path(temporary)
                shutil.copytree(root / 'project', stage / 'project')
                try:
                    yield stage, meta
                finally:
                    if inventory(root / 'project') != original:
                        raise KiCadError('work_copy_changed')

    def simulate_circuit(self, arguments):
        if ('work_copy_id' in arguments) == ('netlist' in arguments):
            raise KiCadError('arguments_unusable')

        def simulate(netlist, directory):
            lines = clean_deck(netlist, arguments.get('model_blocks', []))
            settings, measurements = arguments['analysis'], arguments['measurements']

            def solve(candidate):
                with tempfile.TemporaryDirectory(prefix='ngspice-', dir=directory) as temporary:
                    run = Path(temporary)
                    deck = build_deck(candidate, settings, measurements)
                    (run / 'circuit.cir').write_text(deck)
                    log = self._engine('ngspice', ['-b', 'circuit.cir'], run)
                    if any(marker in log.lower() for marker in (
                            'timestep too small', 'singular matrix', 'no convergence',
                            'simulation(s) aborted', 'fatal error', 'error on line')):
                        raise KiCadError('engine_failed', {'engine': 'ngspice', 'log': log, 'netlist': deck})
                    curves = {}
                    for index, measurement in enumerate(measurements):
                        path = run / f'm{index}.txt'
                        if not path.is_file():
                            raise KiCadError('invalid_report', {'engine': 'ngspice', 'log': log, 'netlist': deck})
                        curves[measurement['id']] = parse_curve(path.read_text(), settings['kind'] == 'ac')
                    from alx.providers.kicad_circuit import MAX_LOG_CHARS
                    return {'curves': curves, 'netlist': deck, 'log': log[-MAX_LOG_CHARS:]}

            result = solve(lines)
            candidates, reasons, omitted = (enumerate_tuning(lines, arguments['tuning'], solve)
                                            if 'tuning' in arguments else ([], [], 0))
            return dict(result, candidates=candidates, candidates_omitted=omitted,
                        status='needs_judgement' if reasons else 'ok',
                        reasons=reasons, conventions={
                            'units': 'SI', 'ac_values': 'complex_peak_phasors',
                            'x': {'ac': 'frequency_hz', 'transient': 'time_seconds',
                                  'operating_point': 'ngspice_op_scale'}[settings['kind']],
                            'impedance': 'one_ampere_positive_injection; other_independent_AC_sources_zero; negative_is_return',
                            'sensitivity': 'one_component_at_a_time_plus_minus_supplied_fraction',
                            'target_sampling': 'linear_interpolation_between_returned_samples'})

        if 'work_copy_id' in arguments:
            with self._analysis_copy(arguments['work_copy_id']) as (stage, meta):
                schematics = list((stage / 'project').glob('*.kicad_sch'))
                if len(schematics) != 1:
                    return {'status': 'needs_judgement', 'reasons': ['schematic_ambiguous'],
                            'curves': {}, 'candidates': [], 'candidates_omitted': 0,
                            'netlist': '', 'log': '', 'conventions': {}}
                output = stage / 'export.cir'
                self._run('sch', 'export', 'netlist', '--format', 'spice', '--output', output, schematics[0])
                return simulate(output.read_text(), stage)
        with tempfile.TemporaryDirectory(prefix='alx-circuit-') as temporary:
            return simulate(arguments['netlist'], Path(temporary))

    def analyse_copper(self, arguments):
        """Coil inductance, resistance and coupling from the board's copper.

        FastHenry runs at two mesh densities so convergence is evidence.
        Self-capacitance is the caller's stated estimate: it is a few pF beside
        a matching network of 100+ pF, and solving it (FastCap) is a follow-up.
        """
        from alx.providers.kicad_circuit import positive
        frequency = positive(arguments['frequency_hz'])
        estimate = positive(arguments['self_capacitance_estimate_f'])
        densities = arguments['mesh_densities']
        ports = arguments['ports']
        if (len(densities) != 2 or any(type(d) is not int or not 1 <= d <= 16 for d in densities)
                or densities[0] >= densities[1] or not 1 <= len(ports) <= 16
                or len({p['id'] for p in ports}) != len(ports)
                or any(not math.isfinite(arguments[k]) or not 0 < arguments[k] <= 1
                       for k in ('convergence_margin', 'formula_margin'))):
            raise KiCadError('arguments_unusable')
        result = {'status': 'needs_judgement', 'reasons': [], 'geometry': [], 'stackup': [],
                  'meshes': [], 'ports': [], 'coupling': [], 'convergence': {}, 'formula': [],
                  'formula_unavailable': [],
                  'conventions': {'units': 'SI', 'frequency_hz': frequency,
                      'current': 'one_ampere_start_to_end_per_port; other_ports_open',
                      'conductivity_s_per_m': 5.8e7,
                      'copper_layer_gaps': 'air; no_filler_specified_by_stackup',
                      'vias': 'vertical bar on the drill; plating_thickness_not_recorded',
                      'net_ties': 'link between the tie pads, width of the smaller pad',
                      'track_joins': 'ends join where their copper overlaps',
                      'solver_grid_m': SOLVER_GRID_M,
                      'fasthenry': 'track_cross_sections; pad_spreading_impedance_not_modelled',
                      'self_capacitance_source': 'stated_estimate',
                      'self_resonance': '1/(2*pi*sqrt(L*C)); quasistatic_single_mode_estimate',
                      'convergence': 'relative to sqrt(Xii*Xjj) of the finer mesh',
                      'convergence_margin': arguments['convergence_margin'],
                      'formula_margin': arguments['formula_margin']}}
        with self._analysis_copy(arguments['work_copy_id']) as (stage, meta):
            tree = sexpr((stage / 'project' / meta['board']).read_text())
            try:
                layers = stackup(tree, require_permittivity=False)
                result['stackup'] = layers
                for density in densities:
                    paths = extract(tree, ports, density, layers)
                    run = stage / f'mesh-{density}'
                    run.mkdir()
                    deck = fasthenry_input(paths, layers, frequency, density)
                    (run / 'copper.inp').write_text(deck)
                    # Direct matrix-vector products: FastHenry 3.0wr's multipole
                    # code crashed (SIGBUS/SIGSEGV) in half of identical runs on
                    # the Quad Coil, and is the approximate method besides.
                    henry_log = self._engine('fasthenry', ['-m', 'direct', 'copper.inp'], run)
                    mesh = {'density': density, 'fasthenry_log': henry_log, 'fasthenry_input': deck,
                            'geometry': [geometry(p, layers) for p in paths]}
                    result['meshes'].append(mesh)
                    if solver_nonconverged(henry_log):
                        result['reasons'].append('solver_nonconvergence')
                        return result
                    self._read_impedance(run, henry_log, ports, frequency, mesh)
                result['geometry'] = result['meshes'][-1]['geometry']
                coarse, fine = result['meshes']
                for quantity in ('resistance_ohm', 'inductance_h'):
                    # Relative to the ports' own terms, sqrt(Xii*Xjj): a mutual
                    # term between nearly uncoupled coils is close to zero, and
                    # its own relative change says nothing about convergence.
                    scale = [abs(fine[quantity][i][i]) for i in range(len(ports))]
                    errors = [[abs(a-b)/max(math.sqrt(scale[i]*scale[j]), 1e-30)
                               for j, (a, b) in enumerate(zip(left, right))]
                              for i, (left, right) in enumerate(zip(coarse[quantity], fine[quantity]))]
                    result['convergence'][quantity] = {'relative_difference': errors,
                                                       'maximum_relative_difference': max(map(max, errors))}
                    if max(map(max, errors)) > arguments['convergence_margin']:
                        result['reasons'].append('mesh_difference:' + quantity)
                omega = 2*math.pi*frequency
                for i, port in enumerate(ports):
                    inductance, resistance = fine['inductance_h'][i][i], fine['resistance_ohm'][i][i]
                    result['ports'].append({'id': port['id'], 'inductance_h': inductance,
                        'resistance_ohm': resistance, 'q': omega*inductance/resistance,
                        'self_capacitance_f': estimate, 'self_capacitance_source': 'stated_estimate',
                        'self_resonant_frequency_hz': 1/(2*math.pi*math.sqrt(inductance*estimate))})
                    try:
                        formula = wheeler(paths[i])
                        difference = abs(inductance-formula['inductance_h'])/formula['inductance_h']
                        result['formula'].append(dict(formula, solver_inductance_h=inductance,
                                                      percentage_difference=100*difference))
                        if difference > arguments['formula_margin']:
                            result['reasons'].append('formula_disagreement:' + port['id'])
                    except GeometryJudgement as error:
                        # The closed form fits square spirals only. A coil it
                        # does not fit is reported, not held as a doubt.
                        result['formula_unavailable'].append({'port': port['id'], 'reason': str(error)})
                    for j in range(i):
                        coupling = fine['inductance_h'][i][j]/math.sqrt(inductance*fine['inductance_h'][j][j])
                        if abs(coupling) > 1+1e-6:
                            raise KiCadError('invalid_report', {'reason': 'nonpassive_coupling'})
                        result['coupling'].append({'ports': [ports[j]['id'], port['id']], 'coefficient': coupling})
                if not result['reasons']:
                    result['status'] = 'facts'
            except GeometryJudgement as error:
                result['reasons'].append(str(error))
            return result

    @staticmethod
    def _read_impedance(run, henry_log, ports, frequency, mesh):
        """FastHenry's impedance matrix into the mesh's resistance and inductance."""
        matrix = run / 'Zc.mat'
        if not matrix.is_file():
            raise KiCadError('invalid_report', {'engine': 'fasthenry', 'log': henry_log})
        impedance = parse_impedance(matrix.read_text(), len(ports), frequency)
        omega = 2*math.pi*frequency
        mesh.update(resistance_ohm=[[z.real for z in row] for row in impedance],
                    inductance_h=[[z.imag/omega for z in row] for row in impedance])
        if any(impedance[i][i].real <= 0 or impedance[i][i].imag <= 0 for i in range(len(ports))):
            raise KiCadError('invalid_report', {'reason': 'nonpassive_copper_matrix'})

    def _source(self, path):
        raw = Path(path).expanduser().absolute()
        if any(raw.resolve().is_relative_to(r) for r in self.design_roots):
            reference_path(raw, roots=self.design_roots)
            return bounded(raw, self.design_roots)
        return reference_path(raw)

    def _board_for(self, arguments):
        """The one board a read names: her work copy, a board path or a project folder."""
        if arguments.get('work_copy_id'):
            root, meta = self._load(arguments['work_copy_id'])
            return root / 'project' / meta['board']
        if arguments.get('board'):
            return bounded(self._source(arguments['board']), self.design_roots)
        if arguments.get('project_dir'):
            project = bounded(self._source(arguments['project_dir']), self.design_roots)
            boards = sorted(project.glob('*.kicad_pcb'))
            if len(boards) != 1:
                raise KiCadError('unsupported_project')
            # The board itself is checked too: a link must not lead outside.
            return bounded(boards[0], self.design_roots)
        raise KiCadError('arguments_unusable')

    def inspect(self, arguments):
        """Every part, net and zone of a board, read from a copy (D-074)."""
        from alx.providers.kicad_section import SectionError, read_overview
        if self.execute_python is None:
            raise KiCadError('engine_unavailable')
        board = self._board_for(arguments)
        try:
            return read_overview(board, arguments, scratch_root=self.work_root / 'sections',
                                 kicad_python=self.settings.kicad_python,
                                 execute_python=self.execute_python)
        except SectionError as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error

    def section(self, arguments):
        """One window of a board as geometry, for the viewer (D-073).

        `board` is a path in a design root (read, never written: it is copied
        into AL/X's storage first); `work_copy_id` is one of her own copies.
        """
        from alx.providers.kicad_section import SectionError, read_section
        if self.execute_python is None:
            raise KiCadError('engine_unavailable')
        board = self._board_for(arguments)
        try:
            return read_section(board, arguments, scratch_root=self.work_root / 'sections',
                                kicad_python=self.settings.kicad_python,
                                execute_python=self.execute_python)
        except SectionError as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error

    def datasheet_section(self, arguments):
        """One section of a datasheet page as a PNG, for the viewer (D-073).

        `document` is a PDF in a design root, read through a copy rendered
        under the KiCad script confinement; it is never changed.
        """
        from alx.providers.page_image import PageError, render_section
        if self.execute_page is None:
            raise KiCadError('engine_unavailable')
        document = arguments.get('document')
        if not isinstance(document, str) or not document.strip():
            raise KiCadError('arguments_unusable', {'detail': 'document is the path of a PDF'})
        document = bounded(document, self.design_roots)
        try:
            return render_section(document, arguments, scratch_root=self.work_root / 'sections',
                                  renderer=self.engine_paths.get('pdftoppm') or Path('/nonexistent'),
                                  execute=self.execute_page)
        except PageError as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error

    def _layout_target(self, arguments):
        """The board a layout check measures, and the project its readings belong to."""
        if bool(arguments.get('work_copy_id')) == bool(arguments.get('board')):
            raise KiCadError('arguments_unusable', {'detail': 'name a work_copy_id or a board, not both'})
        if arguments.get('work_copy_id'):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('library'):
                # One footprint of a library copy, on a board of its own (part FP).
                return self._footprint_board(root, arguments.get('footprint')), meta['source'] + '|' + str(
                    arguments.get('footprint'))
            if arguments.get('footprint'):
                raise KiCadError('arguments_unusable', {'detail': 'footprint is for a library copy'})
            return root / 'project' / meta['board'], meta['source']
        board = bounded(self._source(arguments['board']), self.design_roots)
        return board, str(board.parent)

    def _footprint_board(self, root, footprint):
        """A library footprint placed alone on a board, as part FP, for measuring."""
        if not isinstance(footprint, str) or footprint.count('/') != 1:
            raise KiCadError('arguments_unusable', {'detail': 'footprint is LIBRARY/NAME, e.g. FireFli_Misc/7488912455'})
        library, name = footprint.split('/')
        found = [path for path in (root / 'project').rglob(library + '.pretty') if path.is_dir()]
        if len(found) != 1 or not (found[0] / (name + '.kicad_mod')).is_file():
            raise KiCadError('arguments_unusable', {'detail': f'no footprint {footprint} in this library copy'})
        digest = hashlib.sha256((found[0] / (name + '.kicad_mod')).read_bytes()).hexdigest()[:24]
        holder = root / 'footprint-boards' / digest
        board = holder / 'footprint.kicad_pcb'
        if board.is_file():
            return board
        holder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(found[0], holder / (library + '.pretty'), dirs_exist_ok=True)
        script = holder.parent / ('place-' + uuid.uuid4().hex + '.py')
        script.write_text(
            'import sys, pcbnew\n'
            'from pathlib import Path\n'
            'board_path = Path(sys.argv[-1])\n'
            'part = pcbnew.FootprintLoad(str(board_path.parent / %r), %r)\n'
            'part.SetReference("FP")\n'
            'part.SetPosition(pcbnew.VECTOR2I(0, 0))\n'
            'board = pcbnew.BOARD()\n'
            'board.Add(part)\n'
            'pcbnew.SaveBoard(str(board_path), board)\n' % (library + '.pretty', name))
        try:
            executed = self.execute_python(str(self.settings.kicad_python), script, board)
        finally:
            script.unlink(missing_ok=True)
        if getattr(executed, 'returncode', 1) or not board.is_file():
            raise KiCadError('section_failed', {'detail': (getattr(executed, 'stderr', '') or '')[-500:]})
        return board

    def _readings_path(self, project, digest, page, part):
        key = hashlib.sha256(f'{project}|{digest}|{page}|{part}'.encode()).hexdigest()[:32]
        return self.work_root / 'readings' / (key + '.json')

    def _layout_basis(self, arguments):
        from alx.providers.page_image import PageError, section_box
        from alx.providers.layout_check import LayoutCheckError, document_digest, extract_words, read_numbers
        if self.execute_page is None:
            raise KiCadError('engine_unavailable')
        board, project = self._layout_target(arguments)
        part = arguments.get('part')
        if not isinstance(part, str) or not part.strip():
            raise KiCadError('arguments_unusable', {'detail': "part is the reference the drawing is for, e.g. AE2"})
        document = arguments.get('document')
        if not isinstance(document, str) or not document.strip():
            raise KiCadError('arguments_unusable', {'detail': 'document is the path of a PDF'})
        document = bounded(document, self.design_roots)
        try:
            page, section = section_box(arguments)
            words = extract_words(document, page, scratch_root=self.work_root / 'sections',
                                  extractor=self.engine_paths.get('pdftotext') or Path('/nonexistent'),
                                  execute=self.execute_page)
        except (PageError, LayoutCheckError) as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error
        numbers = read_numbers(words, section)
        path = self._readings_path(project, document_digest(document), page, part)
        stored = json.loads(path.read_text()) if path.is_file() else {'entries': {}, 'rf': False,
                                                                     'confirmed': None}
        return board, document, page, section, part, numbers, path, stored

    def make_hierarchical(self, arguments):
        """Her copy's separate top-level sheets made into one hierarchical design.

        Proven by KiCad's own netlist, before and after: if a single
        connection or net name differs, the copy is put back as it was.
        """
        from alx.providers.kicad_hierarchy import (HierarchyError, connect_with_sheet_pins, make_hierarchical,
                                                   netlist_nets)
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            project = root / 'project'
            schematic = (project / meta['board']).with_suffix('.kicad_sch')
            if not schematic.is_file():
                raise KiCadError('unsupported_project', {'detail': 'no schematic beside the board'})

            def netlist():
                output = root / 'artifacts' / (uuid.uuid4().hex + '.net')
                output.parent.mkdir(exist_ok=True)
                self._run('sch', 'export', 'netlist', '--output', output, schematic)
                return netlist_nets(output.read_text(errors='replace'))
            before = netlist()
            snapshot = root / ('snapshot-' + uuid.uuid4().hex)
            shutil.copytree(project, snapshot)
            try:
                try:
                    result = make_hierarchical(project)
                    # Then the sheets' connections drawn as Altium showed them:
                    # pins on the blocks, wires between (Friedl, 2026-11-13).
                    pins = connect_with_sheet_pins(project)
                except HierarchyError as error:
                    raise KiCadError(error.code, {'detail': error.detail}) from error
                result = dict(result, connected=pins['connected'],
                              reason=result['reason'] or pins['reason'])
                after = netlist() if result['converted'] or pins['connected'] else before
                differing = sorted({before.get(k, '') for k in set(before) - set(after)}
                                   | {after.get(k, '') for k in set(after) - set(before)}
                                   | {before[k] for k in set(before) & set(after) if before[k] != after[k]})
                if differing:
                    raise KiCadError('verification_failed', {'detail': 'the netlist changed; the copy is back '
                                                             'as it was', 'nets_differing': differing[:20]})
                # What the joined design now shows: the board against its whole
                # schematic, and the ERC of every sheet (review of #239). Inside
                # the undo, so a failed check never leaves the change unreported.
                checked = self._drc(root, meta)
                erc = self._erc(root, meta)
            except BaseException:
                shutil.rmtree(project)
                shutil.copytree(snapshot, project)
                raise
            finally:
                shutil.rmtree(snapshot, ignore_errors=True)
            return dict(result, nets=len(after), netlist_identical=True,
                        schematic_parity=checked['schematic_parity'], erc=erc,
                        work_copy_id=arguments['work_copy_id'], work_copy_revision=self._revision(root))

    def check_layout(self, arguments):
        """Her copy, or his board, measured against the manufacturer's drawing (2026-11-13)."""
        from alx.providers.layout_check import MAX_NUMBERS, compare, label, measure_board, measurement
        board, document, page, section, part, numbers, path, stored = self._layout_basis(arguments)
        known = {number['id'] for number in numbers}
        entries = dict(stored['entries'])
        changed = False
        for reading in arguments.get('readings') or ():
            number = reading.get('number')
            if reading.get('derived'):
                # A position the drawing shows without printing a number (a
                # shunt halfway between its series parts): her expected value
                # and its basis, measured by KiCad and shown as derived.
                derived = reading['derived']
                value = derived.get('value_mm') if isinstance(derived, Mapping) else None
                basis = derived.get('basis') if isinstance(derived, Mapping) else None
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not isinstance(basis, str) or not basis.strip()):
                    raise KiCadError('arguments_unusable', {'detail': 'derived has value_mm and a basis'})
                number = 'derived:' + str(number)[:40]
                try:
                    measurement(reading.get('measures'))
                except ValueError as error:
                    raise KiCadError('arguments_unusable', {'detail': f'{number}: {error}'}) from error
                entry = {'measures': dict(reading['measures']), 'label': label(reading['measures']),
                         'derived': {'value_mm': float(value), 'basis': basis.strip()[:120]}}
                changed = changed or entries.get(number) != entry
                entries[number] = entry
                continue
            if number not in known:
                raise KiCadError('arguments_unusable', {'detail': f'no number {number} on this drawing'})
            if reading.get('not_applicable'):
                entry = {'not_applicable': str(reading['not_applicable'])[:120]}
            else:
                try:
                    measurement(reading.get('measures'))
                except ValueError as error:
                    raise KiCadError('arguments_unusable', {'detail': f'{number}: {error}'}) from error
                entry = {'measures': dict(reading['measures']), 'label': label(reading['measures'])}
            changed = changed or entries.get(number) != entry
            entries[number] = entry
        if 'rf' in arguments:
            changed = changed or bool(arguments['rf']) != bool(stored.get('rf'))
            stored['rf'] = bool(arguments['rf'])
        if changed:
            # A changed reading needs his confirmation again.
            stored['confirmed'] = None
        stored['entries'] = entries
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored))
        for key, entry in entries.items():
            if key.startswith('derived:'):
                numbers.append({'id': key, 'text': '%g' % entry['derived']['value_mm'],
                                'value': entry['derived']['value_mm'], 'at': None, 'unit': '',
                                'basis': entry['derived']['basis']})
        known = {number['id'] for number in numbers}
        to_measure = {key: measurement(entry['measures']) for key, entry in entries.items()
                      if key in known and entry.get('measures') is not None}
        measured = {}
        state = self._board_state(arguments)
        if to_measure:
            from alx.providers.kicad_section import SectionError
            try:
                measured = measure_board(board, part, to_measure, scratch_root=self.work_root / 'sections',
                                         kicad_python=self.settings.kicad_python,
                                         execute_python=self.execute_python)
            except SectionError as error:
                raise KiCadError(error.code, {'detail': error.detail}) from error
        if self._board_state(arguments) != state:
            # Measured while the board changed (review of #239): never recorded as current.
            raise KiCadError('work_copy_changed', {'detail': 'the board changed while it was measured; check again'})
        # Every number is counted, not only those listed (review of #239).
        rows = compare(numbers, entries, measured)
        counts = {name: sum(row['status'] == name for row in rows)
                  for name in ('matches', 'differs', 'unaccounted', 'not_applicable')}
        digest = self._readings_digest(stored)
        confirmed = stored.get('confirmed') == digest
        printed = any(not row['id'].startswith('derived:') for row in rows)
        reason = ('' if printed else 'no number could be read in this section: a drawing that is a '
                  'picture, or a section that misses it; nothing was checked against it')
        result = {'check_id': uuid.uuid4().hex, 'work_copy_id': arguments.get('work_copy_id') or '',
                  'document': document.name, 'document_path': str(document),
                  'page': page, 'section': list(section), 'part': part, 'rf': bool(stored.get('rf')),
                  'confirmed': confirmed, 'counts': counts, 'reason': reason,
                  'passed': printed and counts['differs'] == 0 and counts['unaccounted'] == 0
                            and (confirmed or not stored.get('rf')),
                  'numbers': rows[:MAX_NUMBERS], 'numbers_omitted': max(0, len(rows) - MAX_NUMBERS)}
        checks = self.work_root / 'layout-checks'
        checks.mkdir(parents=True, exist_ok=True)
        # What was measured, and on which readings, so the screen can tell a
        # check of an older board or of readings since changed.
        (checks / (result['check_id'] + '.json')).write_text(json.dumps(
            dict(result, measured_state=state, board=arguments.get('board') or '',
                 footprint=arguments.get('footprint') or '',
                 readings_path=str(path), readings_digest=digest)))
        return result

    def _board_state(self, arguments):
        if arguments.get('work_copy_id'):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('library'):
                # The footprint's own file, so a change to it makes a check stale.
                library, _, name = str(arguments.get('footprint', '')).partition('/')
                found = [path for path in (root / 'project').rglob(library + '.pretty') if path.is_dir()]
                return sha(found[0] / (name + '.kicad_mod')) if len(found) == 1 else ''
            return self._revision(root)
        board, _ = self._layout_target(arguments)
        return sha(board)

    @staticmethod
    def _readings_digest(stored):
        return hashlib.sha256(json.dumps([stored.get('entries'), bool(stored.get('rf'))],
                                         sort_keys=True).encode()).hexdigest()

    def _stored_check(self, check_id):
        if not isinstance(check_id, str) or not re.fullmatch(r'[0-9a-f]{32}', check_id):
            return None
        path = self.work_root / 'layout-checks' / (check_id + '.json')
        return json.loads(path.read_text()) if path.is_file() else None

    def _stored_readings(self, result):
        path = Path(result.get('readings_path') or '')
        if not path.is_file() or not path.resolve().is_relative_to((self.work_root / 'readings').resolve()):
            return None, None
        return path, json.loads(path.read_text())

    def confirm_layout_readings(self, arguments):
        """Friedl confirms the readings of the check he saw (RF and high-speed analog parts).

        The approval names that check, and the readings it showed are the
        ones confirmed: if they have changed since, nothing is (review of #239).
        """
        result = self._stored_check(arguments.get('check_id'))
        if result is None:
            raise KiCadError('arguments_unusable', {'detail': 'check_id is the layout check he saw'})
        path, stored = self._stored_readings(result)
        if stored is None or not stored.get('entries'):
            raise KiCadError('arguments_unusable', {'detail': 'that check has no readings to confirm'})
        if self._readings_digest(stored) != result.get('readings_digest'):
            raise KiCadError('arguments_unusable', {'detail': 'the readings changed after that check; '
                                                    'show him the new check and confirm that one'})
        stored['confirmed'] = result['readings_digest']
        path.write_text(json.dumps(stored))
        return {'confirmed': True, 'readings': len(stored['entries'])}

    def layout_check_result(self, check_id):
        """A stored layout check, for the screen; None when there is no such check."""
        result = self._stored_check(check_id)
        if result is None:
            return None
        try:
            target = ({'work_copy_id': result['work_copy_id'], 'footprint': result.get('footprint', '')}
                      if result.get('work_copy_id') else {'board': result.get('board', '')})
            _, stored = self._stored_readings(result)
            # Current only for the same board and the same readings, with the
            # same confirmation (review of #239).
            result['current'] = (self._board_state(target) == result.get('measured_state')
                                 and stored is not None
                                 and self._readings_digest(stored) == result.get('readings_digest')
                                 and (stored.get('confirmed') == result.get('readings_digest'))
                                 == bool(result.get('confirmed')))
        except (KiCadError, OSError, ValueError):
            result['current'] = False
        return result

    def read_reference(self, arguments):
        raw = Path(arguments['path']).expanduser().absolute()
        if not raw.resolve().is_relative_to(self.work_root):
            return read_reference(arguments)
        # Inside AL/X storage only retained edit evidence is readable.
        parts = raw.resolve().relative_to(self.work_root).parts
        if len(parts) < 2 or parts[1] != 'evidence':
            raise KiCadError('path_denied')
        root, _ = self._load(parts[0])
        return read_reference(arguments, roots=(root / 'evidence',), protected=False)

    @contextmanager
    def _locked(self, work_copy_id):
        root, _ = self._load(work_copy_id)
        fd = os.open(root / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KiCadError('work_copy_busy') from error
            yield
        finally:
            os.close(fd)

    def edit(self, arguments):
        with self._locked(arguments['work_copy_id']):
            script = arguments['script']
            if not script or len(script) > 128000 or self.execute_python is None:
                raise KiCadError('arguments_unusable')
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            project = root / 'project'
            library = bool(meta.get('library'))
            # A library copy has no board: the script's argument is its folder.
            board = project / ('.library' if library else meta['board'])
            original = inventory(project)
            snapshot = root / ('snapshot-' + uuid.uuid4().hex)
            shutil.copytree(project, snapshot)
            if inventory(snapshot) != original:
                raise KiCadError('verification_failed')
            self._save_metadata(root, dict(meta, taint=uuid.uuid4().hex))
            empty = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
            script_path = root / ('edit-' + uuid.uuid4().hex + '.py')
            stage = 'before_drc'
            evidence, kept = None, False
            try:
                before = empty if library else self._drc(root, meta)['totals']
                facts_before = board_facts('(kicad_pcb)' if library else board.read_text())
                # Tested helpers first, then her script compiled on its own, so
                # its own first lines (a __future__ import) and line numbers
                # stay as she wrote them (2026-11-12).
                script_path.write_text(HELPERS + script_runner(script))
                stage = 'script'
                executed = self.execute_python(str(self.settings.kicad_python), script_path, board)
                stdout, stderr = (getattr(executed, key, '') or '' for key in ('stdout', 'stderr'))
                if executed.returncode:
                    detail = {'stdout': stdout[:16000], 'stderr': stderr[:16000]}
                    # A signal is KiCad itself crashing, not a Python error
                    # (2026-11-12: four segmentation faults in two minutes).
                    if executed.returncode < 0 or executed.returncode >= 128:
                        detail['crashed'] = (
                            'KiCad crashed while running the script (exit %d). Usual causes: a pcbnew '
                            'call on something that is not there (an outline index of an empty shape, '
                            'a removed item) or an item removed with Remove; KiCad does not check. '
                            'Use the helpers (remove, remove_graphic, copper_shapes, zone_corners).'
                            % executed.returncode)
                    hint = script_hint(stderr)
                    if hint:
                        detail['hint'] = hint
                    raise KiCadError('script_failed', detail)
                inventory(project)
                stage = 'after_drc'
                after = empty if library else self._drc(root, meta)['totals']
                current = design_inventory(project)
                facts_after = board_facts('(kicad_pcb)' if library else board.read_text())
                changed = sorted(name for name in design_inventory(snapshot).keys() | current.keys()
                                 if original.get(name) != current.get(name))
                report = {'files_changed': changed[:200], 'files_truncated': len(changed) > 200,
                          'board': structural_diff(facts_before, facts_after)}
                # Friedl's taper rule on the edited board, so she sees a step
                # before she shows anything (2026-11-12, finished 2026-11-13).
                report['layout_rules'] = ({'findings': [], 'omitted': 0, 'not_checked': 'a library has no board'}
                                          if library else self._layout_rules(board))
                if not library and any(name.endswith('.kicad_sch') for name in changed):
                    # A schematic edit is checked like the first copy (review of #239).
                    report['erc'] = self._erc(root, meta)
                diffs, budget = [], 24000
                for name in changed[:200]:
                    left = (snapshot / name).read_text(errors='replace') if name in original else ''
                    right = (project / name).read_text(errors='replace') if name in current else ''
                    # Stream the diff into a bounded report; retain the snapshot
                    # and edited files as the full evidence behind a truncation.
                    pieces, used, truncated = [], 0, False
                    for line in difflib.unified_diff(left.splitlines(True), right.splitlines(True),
                                                      fromfile=name + ':before', tofile=name + ':after'):
                        take = min(len(line), max(0, budget - used))
                        pieces.append(line[:take])
                        used += take
                        if take < len(line):
                            truncated = True
                            break
                    diffs.append({'name': name, 'diff': ''.join(pieces), 'truncated': truncated})
                    budget -= used
                    if budget <= 0:
                        break
                truncated = len(diffs) < len(changed) or any(d['truncated'] for d in diffs)
                report.update(file_diffs=diffs, diffs_truncated=truncated)
                if truncated:
                    # The report is cut short: keep before and after copies and the
                    # complete diff as evidence that later edits never delete.
                    # The snapshot itself becomes the before copy, still under the taint.
                    evidence = root / 'evidence' / ('edit-' + uuid.uuid4().hex)
                    evidence.mkdir(parents=True)
                    try:
                        shutil.copytree(project, evidence / 'after')
                        with (evidence / 'changes.diff').open('w') as stream:
                            for name in changed:
                                left = (snapshot / name).read_text(errors='replace') if name in original else ''
                                right = (project / name).read_text(errors='replace') if name in current else ''
                                stream.writelines(difflib.unified_diff(
                                    left.splitlines(True), right.splitlines(True),
                                    fromfile=name + ':before', tofile=name + ':after'))
                        if evidence_bytes(root / 'evidence') + evidence_bytes(snapshot) > MAX_EVIDENCE_BYTES:
                            raise KiCadError('evidence_full')
                    except BaseException:
                        shutil.rmtree(evidence, ignore_errors=True)
                        raise
                    report.update(full_diff_path=str(evidence / 'changes.diff'),
                                  before_copy_path=str(evidence / 'before'),
                                  after_copy_path=str(evidence / 'after'))
                script_path.unlink(missing_ok=True)
                revision = self._revision(root, meta)
                if evidence:
                    # Restoring after this point uses the moved snapshot.
                    snapshot.rename(evidence / 'before')
                    snapshot = evidence / 'before'
                # The pending taint is cleared last; nothing after it can fail the edit.
                self._save_metadata(root, meta)
                kept = bool(evidence)
                return {'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': revision,
                        'stdout': stdout[:16000], 'stderr': stderr[:16000],
                        'change_report': report, 'before': before, 'after': after}
            except BaseException as error:
                restored = False
                try:
                    # Never follow a script-created symlink while restoring.
                    if project.is_symlink():
                        project.unlink()
                    elif project.exists():
                        shutil.rmtree(project)
                    shutil.copytree(snapshot, project)
                    restored = inventory(project) == original
                    if getattr(error, 'details', {}).get('reason_code') == 'session_unstoppable':
                        restored = False
                    if restored:
                        self._save_metadata(root, meta)
                except (OSError, KiCadError):
                    restored = False
                if restored and evidence:
                    # The undone edit leaves no evidence; unverified recovery keeps it.
                    shutil.rmtree(evidence, ignore_errors=True)
                recovery = dict(getattr(error, 'recovery', None) or {},
                    work_copy_id=arguments['work_copy_id'], rollback_status='restored' if restored else 'failed',
                    work_copy_tainted=not restored, baseline_eligible=restored, stage=stage)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                code = str(error) if isinstance(error, KiCadError) else (
                    'cli_timeout' if isinstance(error, KICAD_CLI_TIMEOUT) else 'verification_failed')
                raise KiCadError(code, recovery) from error
            finally:
                # Best-effort cleanup only: the outcome is already decided.
                with suppress(OSError):
                    script_path.unlink(missing_ok=True)
                # Retained evidence and unverified recovery keep their snapshot.
                with suppress(OSError, ValueError):
                    if not kept and not json.loads((root / 'metadata.json').read_text()).get('taint'):
                        shutil.rmtree(snapshot, ignore_errors=True)

    def export(self, arguments):
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            relative = Path(arguments['output_dir'])
            if (relative.is_absolute() or not relative.parts
                    or any(p in ('.', '..') for p in relative.parts)
                    # What KiCad keeps beside the design is never written to (review of #208).
                    or self._source(meta['source']) / relative.parts[0] in not_design(
                        self._source(meta['source']))):
                raise KiCadError('path_denied')
            source = self._source(meta['source'])
            output = source / relative
            if not output.resolve().is_relative_to(source):
                raise KiCadError('path_denied')
            self._source(output)
            staging = root / 'artifacts' / ('manufacturing-' + uuid.uuid4().hex)
            staging.mkdir(parents=True)
            board = root / 'project' / meta['board']
            layers = sexpr(board.read_text()).one('layers')
            self._run('pcb', 'export', 'gerbers', '--board-plot-params',
                      '--output', str(staging) + '/', board)
            drill_options = ['--excellon-separate-th'] if arguments.get('separate_drills', False) else []
            drill_origin = arguments.get('drill_origin', 'absolute')
            if drill_origin not in ('absolute', 'plot'):
                raise KiCadError('arguments_unusable')
            drill_options += ['--drill-origin', drill_origin]
            self._run('pcb', 'export', 'drill', *drill_options, '--output', str(staging) + '/', board)
            generated = inventory(staging)
            if not generated:
                raise KiCadError('invalid_report')
            renames = arguments.get('renames', {})
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in renames.items()):
                raise KiCadError('arguments_unusable')
            mapping = {}
            for key, value in renames.items():
                exact_filename(value)
                candidates = [key] if key in generated else []
                if not candidates:
                    matching = [n for n in layers.children if isinstance(n, Node)
                                and key in n.children[1:]]
                    if len(matching) == 1:
                        aliases = [str(n).replace('.', '_') for n in matching[0].children[1:]
                                   if n != 'user' and n != 'signal']
                        candidates = [name for name in generated if Path(name).stem in {
                            board.stem + '-' + layer for layer in aliases}]
                if len(candidates) != 1 or candidates[0] in mapping:
                    raise KiCadError('rename_ambiguous')
                mapping[candidates[0]] = value
            files = {}
            filename_keys = set()
            jobs = set()
            for name in generated:
                final = exact_filename(mapping.get(name, name))
                if name.endswith('.gbrjob'):
                    jobs.add(final)
                key = unicodedata.normalize('NFC', final).casefold()
                if key in filename_keys:
                    raise KiCadError('rename_ambiguous')
                filename_keys.add(key)
                files[final] = read_bytes(staging / name)
            for name in jobs:
                job = json.loads(files[name])
                for entry in job.get('FilesAttributes', []):
                    if 'Path' in entry:
                        entry['Path'] = mapping.get(entry['Path'], entry['Path'])
                files[name] = (json.dumps(job, indent=2) + '\n').encode()
            comparison = compare_manufacturing(files, arguments['compare_with']) if arguments.get('compare_with') else {'files': [], 'truncated': False}
            # Keep final names in staging as well as in the publication.
            final_dir = staging / 'publish'
            final_dir.mkdir()
            for name, data in files.items():
                (final_dir / name).write_bytes(data)
            if 'zip_name' in arguments:
                name = exact_filename(arguments['zip_name'])
                if unicodedata.normalize('NFC', name).casefold() in filename_keys:
                    raise KiCadError('rename_ambiguous')
                with zipfile.ZipFile(final_dir / name, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
                    for entry in files:
                        bundle.write(final_dir / entry, entry)
                files[name] = read_bytes(final_dir / name)
            # Directory descriptors exclude symlink substitution between validation
            # and publication. O_EXCL excludes replacement even after preflight.
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            written = []
            completed = {}

            def record(exports):
                published = dict(meta.get('exports', {}))
                published.update(exports)
                self._save_metadata(root, dict(meta, exports=published))
            try:
                for part in relative.parts:
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                for name in files:
                    try:
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise KiCadError('export_would_overwrite')
                for name, data in files.items():
                    try:
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=descriptor)
                    except FileExistsError as error:
                        raise KiCadError('export_would_overwrite') from error
                    written.append(str(output / name))
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    completed[(relative / name).as_posix()] = hashlib.sha256(data).hexdigest()
                # These exact add-only artifacts are known changes made by AL/X,
                # not changes to the original board/project snapshot. Keep their
                # digests separate so subsequent write-back still detects edits
                # to them or any other change in the original project.
                record(completed)
            except Exception as error:
                # Fully written files stay known, so write-back and a retried
                # export recognise them; a partial file is never recorded.
                if completed:
                    try:
                        record(completed)
                    except OSError:
                        pass
                raise KiCadError(str(error) if isinstance(error, KiCadError) else 'unavailable',
                                 {'files_written': written, 'staging_path': str(staging)}) from error
            finally:
                os.close(descriptor)
            return {'files_written': written, 'comparison': comparison, 'staging_path': str(staging)}

    def _run(self, *args):
        subcommand = ' '.join(args[:3]) if args[:2] == ('pcb', 'export') else ' '.join(args[:2])
        try:
            result = self.execute_cli(str(self.settings.kicad_cli), list(map(str, args)))
        except KICAD_CLI_TIMEOUT as error:
            logger.warning('kicad-cli %s timed out', subcommand)
            raise KiCadError('cli_timeout') from error
        except KICAD_CLI_UNAVAILABLE_ERRORS as error:
            logger.warning('kicad-cli %s unavailable; error: %s', subcommand, type(error).__name__)
            raise KiCadError('unavailable') from error
        if result.returncode != 0:
            logger.warning('kicad-cli %s failed (%s)', subcommand, result.returncode)
            raise KiCadError('cli_failed')

    def _load(self, work_copy_id):
        if not isinstance(work_copy_id, str) or len(work_copy_id) != 32 or any(
                c not in '0123456789abcdef' for c in work_copy_id):
            raise KiCadError('path_denied')
        root = bounded(self.work_root / work_copy_id, (self.work_root,), protected=False)
        # Retained evidence is checked on its own, so it never blocks later work.
        inventory(root, exclude={root / 'evidence'})
        evidence_bytes(root / 'evidence')
        meta = json.loads((root / 'metadata.json').read_text())
        return root, meta

    def _revision(self, root, meta=None):
        # KiCad writes its local settings file beside the board on every save,
        # in her copy too; it is not the design, so it changes nothing here
        # (2026-11-13: it refused a write-back as an unknown changed file).
        contents = design_inventory(root / 'project')
        if meta is None:
            meta = json.loads((root / 'metadata.json').read_text())
        taint = meta.get('taint')
        return hashlib.sha256(json.dumps([contents, taint], sort_keys=True).encode()).hexdigest()

    def _save_metadata(self, root, meta):
        # An interrupted metadata update must leave the prior taint readable.
        target = root / ('metadata-' + uuid.uuid4().hex + '.json')
        try:
            target.write_text(json.dumps(meta))
            os.replace(target, root / 'metadata.json')
        finally:
            target.unlink(missing_ok=True)

    def _layout_rules(self, board):
        """Friedl's layout rules on a board; a check that cannot run says so, never passes."""
        try:
            return check_layout_rules(board, scratch_root=self.work_root / 'sections',
                                      kicad_python=self.settings.kicad_python,
                                      execute_python=self.execute_python)
        except Exception as error:  # noqa: BLE001 - a fact for her, not a failed step
            return {'findings': [], 'omitted': 0, 'not_checked': str(error)[:200]}

    def _erc(self, root, meta):
        """KiCad's electrical rules check of the copy's schematic, grouped like the DRC."""
        schematic = (root / 'project' / meta['board']).with_suffix('.kicad_sch')
        if not schematic.is_file():
            return {'checked': False, 'reason': 'no schematic beside the board', 'totals':
                    {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}, 'groups': [],
                    'top_level_sheets': 0}
        output = root / 'artifacts' / (uuid.uuid4().hex + '.json')
        output.parent.mkdir(exist_ok=True)
        self._run('sch', 'erc', '--format', 'json', '--severity-all', '--output', output, schematic)
        try:
            report = json.loads(output.read_text())
            sheets = list(report['sheets'])
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise KiCadError('invalid_report') from error
        totals = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
        groups = {}
        for sheet in sheets:
            for finding in sheet.get('violations') or ():
                kind, severity = finding.get('type'), finding.get('severity')
                if not isinstance(kind, str) or severity not in ('error', 'warning', 'ignore'):
                    raise KiCadError('invalid_report')
                totals[severity] += 1
                totals['total'] += 1
                items = []
                for item in (finding.get('items') or ())[:ITEMS_PER_FINDING]:
                    description = str(item.get('description', ''))
                    match = re.search(r'\bSymbol ([A-Za-z#]+[0-9?]+)', description)
                    items.append({'description': description,
                                  'designator': match.group(1) if match else '',
                                  'position': item.get('pos', {})})
                entry = groups.setdefault(kind, {'check_type': kind, 'count': 0, 'severity': severity,
                                                 'findings': []})
                entry['count'] += 1
                if severity == 'error':
                    entry['severity'] = 'error'
                entry['findings'].append({'severity': severity, 'sheet': str(sheet.get('path', '')),
                                          'description': str(finding.get('description', '')),
                                          'items': items})
        ordered = sorted(groups.values(), key=lambda entry: (entry['severity'] != 'error', -entry['count']))
        for entry in ordered:
            entry['omitted_findings'] = max(0, len(entry['findings']) - FINDINGS_PER_CHECK_TYPE)
            entry['findings'] = entry['findings'][:FINDINGS_PER_CHECK_TYPE]
        return {'checked': True, 'reason': '', 'totals': totals, 'groups': ordered,
                'top_level_sheets': top_level_sheets(schematic.parent)}

    def _drc(self, root, meta):
        output = root / 'artifacts' / (uuid.uuid4().hex + '.json')
        output.parent.mkdir(exist_ok=True)
        # The board against its schematic too: without this option KiCad never
        # compared them, and schematic_parity was always empty (2026-11-13).
        schematic = (root / 'project' / meta['board']).with_suffix('.kicad_sch')
        self._run('pcb', 'drc', '--format', 'json', '--severity-all', '--refill-zones',
                  *(('--schematic-parity',) if schematic.is_file() else ()),
                  '--output', output, root / 'project' / meta['board'])
        try:
            report = json.loads(output.read_text())
        except (ValueError, OSError) as error:
            raise KiCadError('invalid_report') from error
        if not isinstance(report, dict) or not all(isinstance(report.get(k), list) for k in
                   ('violations', 'unconnected_items', 'schematic_parity')):
            raise KiCadError('invalid_report')
        board_text = (root / 'project' / meta['board']).read_text()
        board = sexpr(board_text)
        identities = {}
        for footprint in board.named('footprint'):
            refs = references(footprint)
            ref = refs[0] if refs else ''
            for node in [footprint, *footprint.named('pad')]:
                for uid in node.named('uuid') + node.named('tstamp'):
                    identities[uid.children[1]] = (ref, node.children[1] if node.name == 'pad' else '')
        totals = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
        citations = {}
        rules_path = (root / 'project' / meta['board']).with_suffix('.kicad_dru')
        rule_names = []
        if rules_path.exists():
            rules = sexpr('(' + rules_path.read_text() + '\n)')
            rule_names = sorted({node.children[1] for node in rules.named('rule')},
                                key=len, reverse=True)

        citation_start = re.compile(r'''\brule\s+(['"])''', re.IGNORECASE)
        # KiCad may append unquoted constraint details before the separator.
        citation_boundary = r'''\s*(?:[;)]|$)|\s+[^'";()]*[;)]'''
        closing_boundary = re.compile(citation_boundary)

        def cited_names(description):
            names = set()
            position = 0
            while start := citation_start.search(description, position):
                quote = start[1]
                position = start.end()
                # A quote (or even a separator) can belong to a loaded name.
                # Prefer the longest complete name with a valid closing boundary.
                for name in rule_names:
                    end = position + len(name) + 1
                    if (description.startswith(name + quote, position)
                            and closing_boundary.match(description, end)):
                        names.add(name)
                        position = end
                        break
                else:
                    # Without a loaded match, use the final valid closing quote
                    # within this citation, never a quote in the next citation.
                    next_start = citation_start.search(description, position)
                    end = next_start.start() if next_start else len(description)
                    match = re.match(r'(.*)' + re.escape(quote) +
                                     r'(?=' + citation_boundary + ')', description[position:end])
                    if match:
                        names.add(match[1])
                        position += match.end()
            return names

        # D-057a: a full report (222,000 characters on RoomReader V2) rode on
        # every later Core call. Each check type keeps its count and its first
        # findings; a noise type keeps two examples. Re-running the check
        # gives the same facts.
        def bounded_groups(groups):
            for entry in groups:
                keep = NOISE_EXAMPLES if entry['noise'] else FINDINGS_PER_CHECK_TYPE
                entry['omitted_findings'] = max(0, len(entry['findings']) - keep)
                entry['findings'] = entry['findings'][:keep]
                for finding in entry['findings']:
                    finding['items'] = finding['items'][:ITEMS_PER_FINDING]
            return groups

        def group(entries):
            groups = {}
            for finding in entries:
                kind, severity = finding['type'], finding['severity']
                if not isinstance(kind, str) or severity not in ('error', 'warning', 'ignore'):
                    raise KiCadError('invalid_report')
                totals[severity] += 1
                totals['total'] += 1
                # Count all citations before finding limits, once per finding/rule.
                for name in cited_names(finding.get('description', '')):
                    citations[name] = citations.get(name, 0) + 1
                items = []
                for item in finding['items']:
                    description = item['description']
                    ref, pad = identities.get(item.get('uuid'), ('', ''))
                    # KiCad item descriptions are an external file format.
                    if not ref:
                        match = re.search(r'\bof ([A-Za-z]+[0-9]+)\b', description)
                        ref = match.group(1) if match else ''
                    if not pad:
                        match = re.search(r'\bPad ([^ ]+)', description)
                        pad = match.group(1) if match else ''
                    items.append({'description': description, 'designator': ref,
                                  'pad_number': pad, 'position': item.get('pos', {}),
                                  'uuid': item.get('uuid', '')})
                entry = groups.setdefault(kind, {'check_type': kind, 'count': 0,
                    'noise': kind in self.settings.noise_checks, 'findings': []})
                entry['count'] += 1
                entry['findings'].append({'severity': severity,
                    'description': finding.get('description', ''), 'items': items})
            return list(groups.values())
        try:
            result = {key: bounded_groups(group(report[key])) for key in
                      ('violations', 'unconnected_items', 'schematic_parity')}
        except (KeyError, TypeError, AttributeError) as error:
            raise KiCadError('invalid_report') from error
        result['totals'] = totals
        result['custom_rule_citations'] = [{'name': name, 'count': count}
                                           for name, count in sorted(citations.items())]
        result['work_copy_tainted'] = bool(meta.get('taint'))
        result['baseline_eligible'] = not bool(meta.get('taint'))
        return result

    def check(self, arguments):
        source = self._source(arguments['project_dir'])
        if source.is_relative_to(self.work_root) or self.work_root.is_relative_to(source):
            raise KiCadError('path_denied')
        if not source.is_dir():
            raise KiCadError('unsupported_project')
        hashes = design_inventory(source)
        boards = [name for name in hashes if '/' not in name and name.endswith('.kicad_pcb')]
        if len(boards) != 1:
            raise KiCadError('unsupported_project')
        work_id = uuid.uuid4().hex
        root = self.work_root / work_id
        root.mkdir(parents=True)
        shutil.copytree(source, root / 'project', ignore=_without_local_history(source))
        if design_inventory(source) != hashes or inventory(root / 'project') != hashes:
            raise KiCadError('original_changed')
        meta = {'source': str(source), 'hashes': hashes, 'board': boards[0]}
        (root / 'metadata.json').write_text(json.dumps(meta))
        result = self._drc(root, meta)
        # The schematic is checked too, errors and warnings, aiming for a
        # clean result like the DRC (Friedl, 2026-11-13).
        result['erc'] = self._erc(root, meta)
        result['layout_rules'] = self._layout_rules(root / 'project' / meta['board'])
        return dict(result, work_copy_id=work_id, work_copy_revision=self._revision(root))

    def copy_library(self, arguments):
        """A work copy of a KiCad library folder (the FireFli library), for footprint and symbol work.

        Edited with the same tested helpers and written back with his approval,
        like a project. On 2026-11-13 hand-written scripts on his library crashed
        three times outside every guard the tool has.
        """
        source = self._source(arguments['library_dir'])
        if source.is_relative_to(self.work_root) or self.work_root.is_relative_to(source) or not source.is_dir():
            raise KiCadError('path_denied' if source.is_dir() else 'unsupported_project')
        hashes = library_inventory(source)
        pretty = sorted({str(Path(name).parent) for name in hashes if name.endswith('.kicad_mod')})
        symbols = sorted(name for name in hashes if name.endswith('.kicad_sym'))
        if not pretty and not symbols:
            raise KiCadError('unsupported_project', {'detail': 'no footprint or symbol library in this folder'})
        work_id = uuid.uuid4().hex
        root = self.work_root / work_id
        root.mkdir(parents=True)
        shutil.copytree(source, root / 'project', ignore=_library_files(source))
        if library_inventory(source) != hashes or library_inventory(root / 'project') != hashes:
            raise KiCadError('original_changed')
        meta = {'source': str(source), 'hashes': hashes, 'board': '', 'library': True}
        (root / 'metadata.json').write_text(json.dumps(meta))
        counts = Counter(str(Path(name).parent) for name in hashes if name.endswith('.kicad_mod'))
        return {'work_copy_id': work_id, 'work_copy_revision': self._revision(root),
                'footprint_libraries': [{'path': name, 'name': Path(name).stem, 'footprints': counts[name]}
                                        for name in pretty],
                'symbol_libraries': symbols}

    def write_back(self, arguments):
        report = arguments.get('report')
        if report is not None and (not isinstance(report, str) or not report.strip() or len(report) > 40000):
            # Refused before anything is written, never after.
            raise KiCadError('arguments_unusable', {'detail': 'report is Markdown of at most 40000 characters'})
        with self._locked(arguments['work_copy_id']):
            recovery = {'backed_up': [], 'written': [], 'rollback_status': 'not_needed',
                        'restorations': [], 'remaining_changed': [], 'cleanup_errors': [],
                        'write_digests': {}, 'conflicts': [],
                        'error': {'code': '', 'type': ''}}
            try:
                written = self._write_back(arguments, recovery)
            except Exception as error:
                code = str(error) if isinstance(error, KiCadError) else 'unavailable'
                if isinstance(error, KiCadError) and error.recovery:
                    recovery.update(error.recovery)
                recovery['error'] = {'code': code, 'type': type(error).__name__}
                raise KiCadError(code, recovery) from error
            if report:
                # His review, kept with the project (Friedl, 2026-11-13), under
                # the approval that wrote the board. The board is written by
                # now: a report that cannot be saved is said, never a failure.
                try:
                    written['report_path'] = str(self._save_review(arguments['project_dir'], report))
                except (KiCadError, OSError) as error:
                    written['report_error'] = (str(getattr(error, 'recovery', None) or error))[:200]
            return written

    def _save_review(self, project_dir, report):
        """A written review in the project's docs/reviews folder: new, never over another."""
        if not isinstance(report, str) or not report.strip() or len(report) > 40000:
            raise KiCadError('arguments_unusable', {'detail': 'report is Markdown of at most 40000 characters'})
        folder = self._source(project_dir)
        project = next((path for path in (folder, *folder.parents)
                        if (path / 'project.json').is_file()), None)
        if project is None or not any(project.is_relative_to(root) for root in self.design_roots):
            project = folder
        reviews = project / 'docs' / 'reviews'
        reviews.mkdir(parents=True, exist_ok=True)
        title = re.sub(r'[^A-Za-z0-9]+', '-', report.strip().splitlines()[0].lstrip('# '))[:60].strip('-')
        stem = time.strftime('%Y-%m-%d') + '-' + (title or 'review')
        for number in range(1, 100):
            target = reviews / (stem + ('' if number == 1 else f'-{number}') + '.md')
            try:
                with open(target, 'x', encoding='utf-8') as handle:
                    handle.write(report.rstrip() + '\n')
                return target
            except FileExistsError:
                continue
        raise KiCadError('unsupported_project', {'detail': 'too many reviews with this title today'})

    def _write_back(self, arguments, recovery):
        root, meta = self._load(arguments['work_copy_id'])
        source = self._source(arguments['project_dir'])
        if str(source) != meta['source']:
            raise KiCadError('path_denied')
        if self._revision(root) != arguments['work_copy_revision']:
            raise KiCadError('work_copy_changed')
        if meta.get('taint'):
            raise KiCadError('work_copy_tainted')

        temporary = set()
        expected = dict(meta['hashes'])
        expected.update(meta.get('exports', {}))

        def validate():
            self._source(source)
            if list(source.rglob('~*.lck')):
                raise KiCadError('board_open')
            current = (library_inventory if meta.get('library') else design_inventory)(source, exclude=temporary)
            if current != expected:
                raise KiCadError('original_changed')
        validate()
        copied = (library_inventory if meta.get('library') else design_inventory)(root / 'project')
        changed = sorted((name for name, value in copied.items() if meta['hashes'].get(name) != value),
                         key=lambda name: (name not in meta['hashes'], name))
        # Any KiCad design file may be written back: a part change edits the
        # schematic as well as the board (2026-11-13, C62 and C66). Anything
        # else, or a file removed, is still refused, and named.
        other = [name for name in changed if not is_design_file(name)]
        missing = sorted(set(meta['hashes']) - set(copied))
        if other or missing:
            raise KiCadError('unsupported_project', {
                'not_design_files': other[:20], 'removed_files': missing[:20]})
        backup = root / 'backups' / uuid.uuid4().hex
        backup.mkdir(parents=True)

        def verified_copy(origin, target, digest):
            shutil.copy2(origin, target)
            if sha(target) != digest or target.read_bytes() != origin.read_bytes():
                raise KiCadError('verification_failed')

        def beside(name, kind):
            target = source / name
            if not target.parent.is_dir():
                raise KiCadError('unsupported_project')
            fd, value = tempfile.mkstemp(prefix='.' + target.name + '.' + kind + '-', dir=target.parent)
            os.close(fd)
            path = Path(value)
            temporary.add(path)
            return path

        staged = {}
        try:
            for name in changed:
                if name in meta['hashes']:
                    target = backup / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    recovery['backed_up'].append(str(target))
                    verified_copy(source / name, target, meta['hashes'][name])
            for name in changed:
                staged[name] = beside(name, 'stage')
                verified_copy(root / 'project' / name, staged[name], copied[name])
            validate()
            if self._revision(root) != arguments['work_copy_revision']:
                raise KiCadError('work_copy_changed')
            for name in changed:
                target = self._source(source / name)
                if sha(staged[name]) != copied[name]:
                    raise KiCadError('verification_failed')
                validate()
                os.replace(staged[name], target)
                recovery['written'].append(str(target))
                recovery['write_digests'][str(target)] = copied[name]
                expected[name] = copied[name]
                if sha(target) != copied[name]:
                    raise KiCadError('verification_failed')
            validate()
            # Success schema predates failure-only digest/conflict evidence.
            return {key: value for key, value in recovery.items()
                    if key not in ('write_digests', 'conflicts')}
        except Exception:
            # POSIX replace/unlink have no compare-digest-and-mutate primitive.
            # Advisory locks do not exclude KiCad or other writers. Even a matching
            # digest cannot authorise a later destructive mutation: another writer
            # could change the path or open inode between those operations. Leave
            # recovery to AL/X with the retained backups; never destroy evidence.
            for value in reversed(recovery['written']):
                target = Path(value)
                name = target.relative_to(source).as_posix()
                digest = None
                try:
                    self._source(target)
                    if target.is_symlink() or not target.is_file():
                        raise KiCadError('path_denied')
                    digest = sha(target)
                    error_code = ('write_digest_mismatch' if digest != recovery['write_digests'][value]
                                  else 'atomic_recovery_unavailable')
                except (OSError, KiCadError):
                    error_code = 'current_contents_unverifiable'
                recovery['conflicts'].append({'original': value, 'reason': error_code,
                    'expected_digest': recovery['write_digests'][value], 'current_digest': digest})
                recovery['restorations'].append({'original': value,
                    'backup': str(backup / name) if name in meta['hashes'] else '',
                    'restored': False, 'error': error_code})
            if recovery['written']:
                recovery['rollback_status'] = 'failed'
            # Include concurrent edits to untouched originals too; never restore those.
            for name in sorted(set(meta['hashes']) | set(changed)):
                target = source / name
                try:
                    self._source(target)
                    if target.is_symlink():
                        different = True
                    else:
                        digest = sha(target) if target.exists() else None
                        different = digest != meta['hashes'].get(name)
                except (OSError, KiCadError):
                    different = True  # Unreadable state cannot be claimed restored.
                if different:
                    recovery['remaining_changed'].append(str(target))
            raise
        finally:
            for path in temporary:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    recovery['cleanup_errors'].append(str(path))
