"""Bounded KiCad mechanics (D-061, amending D-058); never interprets user language."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
import difflib
import unicodedata
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from types import SimpleNamespace
import fcntl

from alx.contracts.kicad import presentation_edit_scope
from alx.providers.credential_paths import denied

# How layout-check readings are measured; part of every confirmation (D-081).
MEASURING = 2
from alx.providers.kicad_helpers import HELPERS, script_runner
from alx.providers.kicad_schematic_helpers import GRID_SUFFIX, SCHEMATIC_HELPERS
from alx.providers.kicad_board_helpers import BOARD_HELPERS
from alx.providers.kicad_rf import RF_HELPERS
from alx.providers.kicad_change import ChangeError, board_state, design_state, plain, set_check, unmet, validate_postconditions
from alx.providers.kicad_style import label_violations, orientation_violations, placement_warnings, violations
from alx.providers.kicad_rules import check_layout_rules
from alx.providers.kicad_errors import errors_to_show
from alx.providers.kicad_steps import (STEPS, edit_refusal, his_own_steps, original_of, routing_added, fingerprints, parity_mismatches, ready,
                                        shown_mismatch,
                                      step_status)
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


def staged_bytes(path):
    """A file the export itself just generated in her own work copy (18 Nov: read through the
    reference reader, whose credential check refuses every path under .alx, every export from
    her real storage failed). Never a link; only a regular file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise KiCadError('path_denied')
        return stream.read()


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


SPICE_MODEL_DIRECTIVES = frozenset({'.model', '.subckt', '.ends', '.param', '.func', '.global', '.end',
                                    '.options', '.option'})


DESIGN_SUFFIXES = frozenset({'.kicad_pcb', '.kicad_pro', '.kicad_dru', '.kicad_sch', '.kicad_sym',
                             '.kicad_mod', '.kicad_wks'})
DESIGN_NAMES = frozenset({'fp-lib-table', 'sym-lib-table'})


_ICLOUD_COPY = re.compile(r'^(?P<stem>.+) (?:[2-9]|[1-9]\d+)(?P<ext>\.[^. ]+)?$')


def icloud_copies(names, folder=None):
    """Files iCloud Drive made beside a file as conflict copies: "fp-lib-table 2", "x 3.kicad_sch".

    Work copies live under the Desktop, which iCloud syncs; on 25 Nov it made over a hundred
    stale copies in a P0009 work copy, which refused the write-back and would have put duplicate
    boards and sheets into his project. A name counts only when the plain name sits beside it,
    and, given the folder, when no other design file names it: a new sheet "Sensor 2" that the
    hierarchy uses is his design (review of #247).
    """
    names = set(names)
    present = names | {'/'.join(name.split('/')[:i]) for name in names
                       for i in range(1, name.count('/') + 1)}
    found = set()
    for name in names:
        parts = name.split('/')
        for i, part in enumerate(parts):
            match = _ICLOUD_COPY.match(part)
            if match and '/'.join(parts[:i] + [match['stem'] + (match['ext'] or '')]) in present:
                found.add(name)
                break
    if folder is not None and found:
        # A copy a kept file names is kept too, and then read in turn: a sheet "A 2" that the
        # top sheet uses may itself use "B 2" (review of #248).
        to_read = names - found
        while to_read and found:
            named = set()
            for name in to_read:
                if Path(name).suffix in ('.kicad_sch', '.kicad_pro') or Path(name).name in DESIGN_NAMES:
                    with suppress(OSError):
                        content = (Path(folder) / name).read_text(errors='replace')
                        # Named in full, as a quoted file or the end of a quoted path: "Charger 2.kicad_sch"
                        # is not named by "MyCharger 2.kicad_sch".
                        named |= {copy for copy in found
                                  if re.search(r'["/]%s"' % re.escape(Path(copy).name), content)}
            found -= named
            to_read = named
    return found


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
            if path in exclude or path.is_symlink() or not _library_file(here, name):
                continue
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES:
                raise KiCadError('project_too_large')
            result[path.relative_to(source).as_posix()] = sha(path)
    return result


def _placed_extent(extent, offset, rotate):
    """A STEP model's bounding box as KiCad places it: rotated about x, y then z (KiCad turns by
    the negative of the stored angles), then offset; in mm, z up."""
    corners = [(x, y, z) for x in (extent[0][0], extent[1][0]) for y in (extent[0][1], extent[1][1])
               for z in (extent[0][2], extent[1][2])]
    for axis, angle in enumerate(rotate):
        a = math.radians(-angle)
        c, s = math.cos(a), math.sin(a)
        turned = []
        for x, y, z in corners:
            if axis == 0:
                turned.append((x, y * c - z * s, y * s + z * c))
            elif axis == 1:
                turned.append((x * c + z * s, y, -x * s + z * c))
            else:
                turned.append((x * c - y * s, x * s + y * c, z))
        corners = turned
    corners = [(x + offset[0], y + offset[1], z + offset[2]) for x, y, z in corners]
    return (tuple(min(p[i] for p in corners) for i in range(3)), tuple(max(p[i] for p in corners) for i in range(3)))


def _non_library_parts(folder):
    """(reference, lib_id) of every placed symbol in a folder's schematics that is not a FireFli
    library part; power symbols and flags (#PWR, #FLG) are not parts."""
    found = []
    for sheet in sorted(Path(folder).rglob('*.kicad_sch')):
        if any(part.startswith('.') or part == 'backups' or part.endswith('-backups')
               for part in sheet.relative_to(folder).parts[:-1]):
            continue
        try:
            source = sheet.read_text(errors='replace')
        except OSError:
            continue
        for match in re.finditer(r'\n\t\(symbol\n\t\t\(lib_id "((?:[^"\\]|\\.)*)"\)(.*?)\n\t\)', source, re.S):
            reference = re.search(r'\(property "Reference" "((?:[^"\\]|\\.)*)"', match.group(2))
            reference = reference.group(1) if reference else '?'
            if not reference.startswith('#') and not match.group(1).startswith('FireFli_'):
                found.append((reference, match.group(1)))
    return found


def _library_file(folder, name):
    """A library's own file: KiCad's, its catalogue (catalogue/*.csv: every part's maker, MPN and
    LCSC number), its 3D models (3dmodels/*.3dshapes/*.step) and SPICE models (spice/*), and the
    generator the resistors and capacitors are built by (generator/*.py, its lcsc.csv), so a part
    added to a copy is added where the next build keeps it (26 Nov 2026)."""
    folder, lower = Path(folder), name.lower()
    return (is_design_file(name) or (folder.name in ('catalogue', 'generator') and lower.endswith('.csv'))
            or (folder.name == 'generator' and lower.endswith('.py'))
            or (folder.name.endswith('.3dshapes') and lower.endswith(('.step', '.stp')))
            or (folder.name == 'spice' and lower.endswith(('.lib', '.cir', '.mod', '.sp', '.spi', '.spice', '.txt',
                                                         '.sub', '.ckt'))))


def _library_files(source):
    """A copytree ignore keeping only a library's KiCad files and their folders."""
    def ignore(directory, names):
        here = Path(directory)
        # Links are never followed: they could lead to far more than the
        # inventory counted (review of #240).
        return [name for name in names if name in _LIBRARY_SKIPPED or (here / name).is_symlink() or (
            not (here / name).is_dir() and not _library_file(here, name))]
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


def flat_symbol(text, tree, node, name, ancestors=()):
    """A library symbol as a schematic stores it: one that extends another (16 Nov: every FireFli
    capacitor extends "C") gets that symbol's drawing and pins under its own name, and keeps
    its own fields."""
    extends = node.named('extends')
    if not extends:
        return text[node.start:node.end]
    base_name = extends[0].children[1]
    base = [n for n in tree.named('symbol') if len(n.children) > 1 and n.children[1] == base_name]
    if not base:
        raise KiCadError('arguments_unusable', {'detail': '%s extends %s, which is not in its library'
                                                % (name, base_name)})
    base = base[0]
    base_text = text
    if base_name in (*ancestors, name):
        raise KiCadError('arguments_unusable', {'detail': f'cyclic symbol inheritance at {base_name}'})
    if base.named('extends'):
        base_text = flat_symbol(text, tree, base, base_name, (*ancestors, name))
        base = sexpr(base_text)
    own_fields = {p.children[1] for p in node.named('property') if len(p.children) > 1}
    own_kinds = {c.name for c in node.children if isinstance(c, Node)}
    own_units = {str(unit.children[1]) for unit in node.named('symbol')}
    added = []
    for child in base.children[2:]:
        if not isinstance(child, Node) or child.name == 'extends':
            continue
        if child.name == 'property' and len(child.children) > 1 and child.children[1] in own_fields:
            continue
        if child.name not in ('property', 'symbol') and child.name in own_kinds:
            continue
        piece = base_text[child.start:child.end]
        if child.name == 'symbol' and len(child.children) > 1:
            unit = str(child.children[1])
            target = name + unit[len(base_name):]
            if target in own_units:
                continue
            piece = piece.replace(json.dumps(unit), json.dumps(target), 1)
        added.append(piece)
    head = text[node.start:extends[0].start].rstrip()
    rest = text[extends[0].end:node.end - 1].rstrip()
    return head + rest + '\n' + '\n'.join('\t\t' + piece for piece in added) + '\n\t)'


# The shape of a kept netlist reading; bumped when netlist_model changes.
NETLIST_VERSION = 3


class KiCadProvider:
    # Set by the runtime: where drawings for looking go, and how KiCad's own
    # windows are opened and the screen captured (open_in_kicad).
    render_folder = Path('/nonexistent')
    open_viewer = None
    capture_screen = None
    kicad_shared = Path('/Applications/KiCad/KiCad.app/Contents/SharedSupport')
    viewer_settle_seconds = 8.0

    def __init__(self, settings, execute_cli, execute_python=None):
        self.settings, self.execute_cli = settings, execute_cli
        self.execute_python = execute_python
        self.design_roots = tuple(p.expanduser().resolve() for p in settings.design_roots)
        # Datasheets may also be in her own storage (ALX_Storage), as her
        # picture tool already allows (2026-11-13: copies to his Desktop).
        self.reference_roots = self.design_roots
        self.datasheet_folder = None
        self.datasheet_sources = None
        self.work_root = settings.work_root.expanduser().resolve()
        self._viewers = []
        self.engine_paths = {}
        self.execute_engine = None
        self.execute_page = None
        # Builds AL/X's own Swift sources (picture_text.swift); None where unavailable.
        self.build_program = None

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

    @staticmethod
    def _is_copy_id(value):
        return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)

    def _project(self, name):
        """A project as he names it: a folder, or just its number (P0009), down to the folder
        that holds its one board. 16 Nov: the agent spent 14 calls looking for "P0009"."""
        text = str(name).strip()
        # A project number is one letter and its digits (P0009): a folder name, not language.
        if len(text) >= 4 and text[0].isascii() and text[0].isalpha() and text[1:].isascii() and text[1:].isdigit():
            found = []
            for root in self.design_roots:
                for depth in ('*', '*/*'):
                    found += [path for path in root.glob(depth) if path.is_dir()
                              and (path.name == text or path.name.lower().startswith(text.lower() + '-'))]
            found = sorted(set(found))
            if not found:
                raise KiCadError('unsupported_project', {'detail': 'no project %s in the design folders' % text})
            if len(found) > 1:
                raise KiCadError('unsupported_project', {'detail': 'more than one project %s: %s' % (
                    text, '; '.join(str(path) for path in found[:5]))})
            folder = found[0]
        else:
            storage = self._storage_root()
            given = Path(text)
            if storage is not None and given.parts == (text,) and given != Path('..'):
                text = str(storage / 'Projects' / text)
            elif given.parts[:2] == ('~', 'ALX_Storage') or given.is_relative_to(Path.home() / 'ALX_Storage'):
                text = str(self._reference_location(text))
            folder = self._source(text)
        conventional = folder / 'hardware' / 'kicad'
        if conventional.is_dir():
            conventional = self._source(conventional)
            boards = self._project_boards(conventional)
            if len(boards) == 1:
                self._source(boards[0])
                return boards[0].parent
            raise KiCadError('unsupported_project', {'detail': self._where_boards_are(folder, boards,
                                                                                      expected=conventional)})
        if folder.is_dir() and not any(folder.glob('*.kicad_pcb')):
            # Preserve the nonstandard layout lookup for existing projects.
            below = {board.parent for board in self._project_boards(folder)}
            if len(below) == 1:
                return self._source(below.pop())
        return folder

    @staticmethod
    def _project_boards(folder):
        return sorted(path for path in folder.rglob('*.kicad_pcb') if path.is_file()
                      and not any('backup' in part.lower() or part == '.history'
                                  for part in path.relative_to(folder).parts[:-1]))

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
            if meta.get('library'):
                # A library footprint is drawn as the land-pattern check
                # measures it: alone on a board of its own, as part FP
                # (14 Nov: the screen could not draw one, so she showed none).
                return self._footprint_board(root, arguments.get('footprint'))
            if arguments.get('footprint'):
                raise KiCadError('arguments_unusable', {'detail': 'footprint is for a library copy'})
            return root / 'project' / meta['board']
        if arguments.get('board'):
            board = str(arguments['board'])
            if not board.endswith('.kicad_pcb'):
                # A project named where a board file was expected (16 Nov: board "P0009").
                return self._board_for({'project_dir': board})
            return bounded(self._source(board), self.design_roots)
        if arguments.get('project_dir'):
            project = bounded(self._project(arguments['project_dir']), self.design_roots)
            boards = sorted(project.glob('*.kicad_pcb'))
            if len(boards) != 1:
                raise KiCadError('unsupported_project', {'detail': self._where_boards_are(project, boards)})
            # The board itself is checked too: a link must not lead outside.
            return bounded(boards[0], self.design_roots)
        raise KiCadError('arguments_unusable')

    @staticmethod
    def _where_boards_are(project, boards, *, expected=None):
        """Name the candidate board paths without choosing between them."""
        if len(boards) > 1 and expected is None:
            return 'this folder holds %d boards: %s' % (len(boards), ', '.join(map(str, boards)))
        candidates = sorted(set(boards) | set(KiCadProvider._project_boards(project)))
        detail = f'expected exactly one KiCad board in {expected}' if expected else 'no KiCad board in this folder'
        return detail + ('; candidates: ' + '; '.join(map(str, candidates)) if candidates else '; candidates: none')

    def inspect(self, arguments):
        """Every part, net and zone of a board, read from a copy (D-074)."""
        from alx.providers.kicad_section import SectionError, read_overview
        if self.execute_python is None:
            raise KiCadError('engine_unavailable')
        board = self._board_for(arguments)
        if arguments.get('brief') and arguments.get('window') is None:
            # Just the parts' names, from the board index: no KiCad process (18 Nov: the full
            # reading of P0009 was too much to take in, so references were guessed in sixes).
            from alx.providers.kicad_index import select
            index = self._board_index(board, self.work_root / 'sections')
            refs = set(arguments.get('refs') or ()) or None
            needle = str(arguments.get('contains') or '').lower()
            # The same filters as the full reading, then only the names (18 Nov review: nets were
            # ignored and fields not searched).
            on_nets = ({o['ref'] for o in select(index, 'pad', nets=arguments['nets'])}
                       if arguments.get('nets') else None)
            parts = [{'ref': o['ref'], 'value': o['value'], 'footprint': o['footprint']}
                     for o in select(index, 'part', refs=refs)
                     if (on_nets is None or o['ref'] in on_nets)
                     and (not needle or needle in ' '.join([o['ref'], o['value'], o['footprint'],
                                                            *map(str, o['fields'].values())]).lower())]
            limit = max(1, min(int(arguments.get('limit') or 300), 1000))
            return {'board': {'file': board.name}, 'parts': parts[:limit], 'nets': [], 'zones': [],
                    'truncated': {'parts': len(parts) > limit}}
        overview_args = {key: value for key, value in arguments.items() if key not in ('window', 'brief')}
        try:
            result = read_overview(board, overview_args, scratch_root=self.work_root / 'sections',
                                   kicad_python=self.settings.kicad_python,
                                   execute_python=self.execute_python)
            if arguments.get('window') is not None:
                result = dict(result, copper=self._copper_in(board, arguments))
            return result
        except SectionError as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error

    def _firefli_library(self):
        """The FireFli library folder: KiCad's FIREFLI_KICAD_LIB path variable (his settings)."""
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        versions = sorted((p for p in (home / 'Library/Preferences/kicad').glob('*') if p.is_dir()),
                          key=lambda p: [int(x) for x in re.findall(r'\d+', p.name)] or [0])
        for config in reversed(versions):
            try:
                found = ((json.loads((config / 'kicad_common.json').read_text())
                         .get('environment') or {}).get('vars') or {}).get('FIREFLI_KICAD_LIB')
            except (OSError, ValueError, AttributeError):
                continue
            if found:
                return Path(found).expanduser()
        raise KiCadError('unavailable', {'detail': 'the FireFli library is not set up in KiCad '
                                                   '(path variable FIREFLI_KICAD_LIB)'})

    def _design_blocks(self):
        try:
            library = self._firefli_library()
        except KiCadError:
            return {}
        folder = library / 'blocks/FireFli_Blocks.kicad_blocks'
        blocks = {}
        for path in sorted(folder.glob('*.kicad_block')):
            name = path.stem
            drawing = path / f'{name}.kicad_sch'
            try:
                if not drawing.resolve().is_relative_to(folder.resolve()) or not drawing.is_file():
                    continue
                metadata = path / f'{name}.json'
                description = ''
                if metadata.resolve().is_relative_to(folder.resolve()) and metadata.is_file():
                    description = json.loads(metadata.read_text()).get('description', '')
                blocks[name] = (drawing, description if isinstance(description, str) else '')
            except (OSError, ValueError, AttributeError):
                continue
        return blocks

    def design_block_catalog(self):
        """Names and descriptions from the configured FireFli design-block library."""
        return {name: description for name, (_, description) in self._design_blocks().items()}

    def find_part(self, arguments):
        """Parts in the FireFli library matching what is asked, with exact symbol and footprint
        names, part number, LCSC number and package (19 Nov). Changes nothing."""
        from alx.providers.kicad_parts import find_parts
        if not any(str(arguments.get(k) or '').strip() for k in ('query', 'value', 'package', 'kind')):
            raise KiCadError('arguments_unusable', {'detail': 'give query, value, package or kind'})
        try:
            limit = int(arguments.get('limit') or 20)
        except (TypeError, ValueError):
            limit = 20
        try:
            found = find_parts(self._firefli_library(), str(arguments.get('query') or ''),
                               str(arguments.get('value') or ''), str(arguments.get('package') or ''),
                               str(arguments.get('kind') or ''), limit)
        except FileNotFoundError as error:
            raise KiCadError('unavailable', {'detail': 'no catalogue at %s' % error}) from error
        except ValueError as error:
            raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
        if not found['count']:
            found['hint'] = ('not in the FireFli library: a new part is created in a copy_library work copy '
                             'from its datasheet, then used by its exact name')
            offered = self._jlc_capacitors(arguments)
            if offered is not None:
                found.update(offered)
                found['hint'] = ('not in the FireFli library yet. jlc_candidates are the capacitors JLC stocks '
                                 'for it under his brand rule: choose one (same or better dielectric, voltage '
                                 'and tolerance than the part it replaces) and add_passive it to a copy_library '
                                 'work copy; write that back with his approval, then swap_part')
        return found

    def _jlc_capacitors(self, arguments):
        """JLC's capacitors for a value and chip size the library lacks, or None when the
        question is not one (no value, no size, or not a capacitor)."""
        from alx.providers.kicad_parts import value_of
        from alx.providers.kicad_passives import CACHE, catalogue_date, jlc_candidates
        kind = str(arguments.get('kind') or '').strip().casefold()
        farads = value_of(str(arguments.get('value') or ''))
        size = re.search(r'\b(0402|0603|0805|1210|1812)\b', str(arguments.get('package') or ''))
        if farads is None or size is None or kind not in ('', 'c', 'capacitor', 'capacitors'):
            return None
        if kind == '' and not re.search(r'(?i)[pnuµ]f?$', str(arguments.get('value')).strip()):
            return None
        cache = self._firefli_library() / CACHE
        try:
            listed = jlc_candidates(cache, farads * 1e12, size.group(1))
        except (FileNotFoundError, OSError):
            return {'jlc_candidates': [], 'jlc_catalogue_date': 'no local JLC catalogue (generator/lookup_lcsc.py makes it)'}
        return {'jlc_candidates': listed, 'jlc_catalogue_date': catalogue_date(cache)}

    def add_passive(self, arguments):
        """A capacitor from JLC's catalogue into a copy_library work copy, through the library's own
        generator: its line in generator/capacitors.py, its LCSC number in generator/lcsc.csv, then the
        generator and KiCad's re-save, exactly as build.sh does (26 Nov 2026)."""
        from alx.providers.kicad_passives import CACHE, decode, jlc_listing, with_entry, with_lcsc_row
        mpn = str(arguments.get('mpn') or '').strip()
        part = decode(mpn)
        if part is None:
            raise KiCadError('arguments_unusable', {'detail': '%r is not a Taiyo Yuden X5R/X7R or YAGEO CC C0G '
                                                    'part number in 0402-1812: take one from find_part\'s '
                                                    'jlc_candidates' % mpn})
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if not meta.get('library'):
                raise KiCadError('arguments_unusable', {'detail': 'add_passive needs a copy_library work copy'})
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            project = root / 'project'
            generator = project / 'generator' / 'capacitors.py'
            table = project / 'generator' / 'lcsc.csv'
            if not generator.is_file() or not table.is_file():
                raise KiCadError('arguments_unusable', {'detail': 'this copy is not the FireFli library (no '
                                                        'generator/capacitors.py and generator/lcsc.csv)'})
            listing = jlc_listing(self._firefli_library() / CACHE, mpn, part['manufacturer'])
            if listing is None:
                raise KiCadError('arguments_unusable', {'detail': 'JLC lists %s not once in its catalogue: take one '
                                                        'from find_part\'s jlc_candidates' % mpn})
            try:
                source = with_entry(generator.read_text(encoding='utf-8'), part, listing['lcsc'])
                rows = with_lcsc_row(table.read_text(encoding='utf-8'), mpn, listing['lcsc'], listing['jlc_library'])
            except ValueError as error:
                raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
            if self.execute_python is None:
                raise KiCadError('engine_unavailable')
            script = root / ('generate-' + uuid.uuid4().hex + '.py')
            script.write_text('import runpy, sys\nsys.path.insert(0, %r)\nrunpy.run_path(%r, run_name="__main__")\n'
                              % (str(generator.parent), str(generator)))
            try:
                with self._change(root, meta):
                    generator.write_text(source, encoding='utf-8')
                    table.write_text(rows, encoding='utf-8')
                    executed = self.execute_python(str(self.settings.kicad_python), script, project / '.library')
                    if executed.returncode:
                        raise KiCadError('script_failed', {'detail': 'the library generator stopped',
                                                           'stderr': (executed.stderr or '')[-4000:]})
                    # KiCad re-saves the files in its own format, which also proves it reads them.
                    self._run('sym', 'upgrade', '--force', project / 'symbols' / 'FireFli_Capacitors.kicad_sym')
                    self._run('fp', 'upgrade', '--force', project / 'footprints' / 'FireFli_Capacitors.pretty')
            finally:
                script.unlink(missing_ok=True)
            return {'symbol': 'FireFli_Capacitors:%s' % mpn,
                    'footprint': 'FireFli_Capacitors:C_%s_%sMetric' % (part['size'], {
                        '0402': '1005', '0603': '1608', '0805': '2012', '1210': '3225', '1812': '4532'}[part['size']]),
                    'part': {k: v for k, v in part.items() if k not in ('pf', 'volts')}, **listing,
                    'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': self._revision(root),
                    'next': 'write_back this library copy with his approval (project_dir is the library folder), '
                            'then swap_part on the board copy'}

    def swap_part(self, arguments):
        """Parts changed to one library part at the same pins, a step of his order per call (26 Nov):
        the schematic symbols first; once he approved the schematic, the board footprints, pads
        keeping their nets and taking the part's fields. Through edit, so every check applies."""
        from alx.providers.kicad_index import board_index
        from alx.providers.kicad_swap import (board_fields, board_script, expand_refs, library_fields,
                                              placed_symbols, schematic_script)
        try:
            refs = expand_refs(arguments.get('refs'))
        except ValueError as error:
            raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
        part = str(arguments.get('part') or '').strip()
        if not refs or not re.fullmatch(r'[^/\\:]+:[^/\\:]+', part):
            raise KiCadError('arguments_unusable', {'detail': 'refs (e.g. ["C94-C96"]) and part, the library '
                                                    'symbol LIBRARY:NAME from find_part'})
        work_copy_id = arguments['work_copy_id']
        root, meta = self._load(work_copy_id)
        if meta.get('library'):
            raise KiCadError('arguments_unusable', {'detail': 'swap_part needs a board work copy'})
        project = root / 'project'
        revision = self._revision(root, meta)
        fields = library_fields(self._library_parts([part], project, 'symbols')[part])
        footprint = fields.get('Footprint', '')
        if not re.fullmatch(r'[^/\\:]+:[^/\\:]+', footprint):
            raise KiCadError('arguments_unusable', {'detail': '%s names no footprint (LIBRARY:NAME)' % part})
        placed = placed_symbols(project)
        unknown = [ref for ref in refs if ref not in placed]
        if unknown:
            raise KiCadError('arguments_unusable', {'detail': 'not on the schematic: %s' % ', '.join(unknown)})
        base = {'work_copy_id': work_copy_id, 'part': part, 'footprint': footprint}
        on_schematic = [ref for ref in refs if placed[ref] != part]
        if on_schematic:
            edited = self.edit({'work_copy_id': work_copy_id, 'work_copy_revision': revision,
                                'script': schematic_script(on_schematic, part), 'symbols': [part],
                                'intent': [{'op': 'swap', 'ref': ref, 'part': part} for ref in on_schematic]})
            return dict(edited, **base, side='schematic', swapped=on_schematic,
                        next='show him the schematic and approve_step schematic; then swap_part again for the board')
        on_board = board_state(board_index((project / meta['board']).read_text()))[0]
        missing = [ref for ref in refs if ref not in on_board]
        if missing:
            raise KiCadError('arguments_unusable', {'detail': 'not on the board: %s (update the board from the '
                                                    'schematic first)' % ', '.join(missing)})
        to_change = [ref for ref in refs if on_board[ref] != footprint]
        if not to_change:
            return dict(base, work_copy_revision=revision, side='none', swapped=[],
                        next='every part named is already %s on the schematic and %s on the board' % (part, footprint))
        status = step_status(self._original_steps(root, meta), meta.get('approvals', {}),
                             fingerprints(project, meta['board']))
        if not ready(status, 'schematic'):
            raise KiCadError('step_not_approved', {'needs': 'schematic', 'steps': status,
                                                   'detail': 'show him the swapped schematic and approve_step '
                                                             'schematic before the board changes'})
        edited = self.edit({'work_copy_id': work_copy_id, 'work_copy_revision': revision,
                            'script': board_script(to_change, footprint, board_fields(fields)),
                            'footprints': [footprint.replace(':', '/', 1)],
                            'intent': [{'op': 'swap', 'side': 'board', 'ref': ref, 'part': footprint}
                                       for ref in to_change]})
        return dict(edited, **base, side='board', swapped=to_change,
                    next='show him the placement (tracks to the old pads may now end short of the new ones) '
                         'and approve_step placement before any routing')

    def select(self, arguments):
        """Find Similar on a board: the objects of one kind matching every filter, from the board
        index of this revision (18 Nov). Changes nothing."""
        from alx.providers.kicad_index import select, view
        revision, library = None, False
        if self._is_copy_id(arguments.get('work_copy_id')):
            # Read under the copy's lock, so the revision it reports is the one it read (19 Nov recheck).
            with self._locked(arguments['work_copy_id']):
                root, meta = self._load(arguments['work_copy_id'])
                library = bool(meta.get('library'))
                board = self._board_for(arguments if library else
                                        {key: value for key, value in arguments.items() if key != 'footprint'})
                scratch, revision = root / 'artifacts', self._revision(root, meta)
                index = self._board_index(board, scratch)
        else:
            # footprint here filters parts; for _board_for it would mean a library preview (review).
            board = self._board_for({key: value for key, value in arguments.items() if key != 'footprint'})
            index = self._board_index(board, self.work_root / 'sections')
        filters = {key: arguments[key] for key in ('refs', 'nets', 'layer', 'name', 'value', 'footprint',
                                                   'area', 'reads', 'ids', 'named') if arguments.get(key) is not None
                   and not (library and key == 'footprint')}
        try:
            if arguments['kind'] == 'view':
                if set(filters) - {'refs', 'nets', 'area'}:
                    raise ValueError('a view takes refs, nets and area only')
                found = view(index, **filters)
            else:
                found = select(index, arguments['kind'], **filters)
        except (TypeError, ValueError) as error:
            raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
        limit = max(1, min(int(arguments.get('limit') or (400 if arguments['kind'] == 'view' else 100)), 500))
        # Polygon outlines can run to thousands of points: shown as their count.
        shown = [{key: (len(value) if key in ('outline', 'points') else value) for key, value in item.items()}
                 for item in found[:limit]]
        counts = dict(Counter(item['kind'] for item in found)) if arguments['kind'] == 'view' else None
        return {'kind': arguments['kind'], 'count': len(found), 'objects': shown,
                'truncated': len(found) > limit, 'board': board.name, **({'counts': counts} if counts else {}),
                **({'work_copy_revision': revision} if revision else {})}

    def _copper_in(self, board, arguments):
        """The copper inside a window, compactly: what a route has to reach (16 Nov: the
        board view gave only counts, so nothing could be routed to an existing track)."""
        from alx.providers.kicad_section import read_section
        section = read_section(board, {'window': arguments['window'], 'nets': arguments.get('nets') or [],
                                       'refs': arguments.get('refs') or []},
                               scratch_root=self.work_root / 'sections', kicad_python=self.settings.kicad_python,
                               execute_python=self.execute_python)
        rounded = (lambda point: [round(float(point[0]), 3), round(float(point[1]), 3)])
        pads = []
        for pad in section.get('pads') or ():
            outline = [p for polygon in pad.get('polygons') or () for p in polygon.get('outer') or ()]
            if not outline:
                continue
            xs, ys = [p[0] for p in outline], [p[1] for p in outline]
            pads.append({'pad': '%s.%s' % (pad.get('ref'), pad.get('number')), 'net': pad.get('net', ''),
                         'centre': rounded(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)),
                         'size': [round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)]})
        return {'window': list(arguments['window']),
                'tracks': [{'points': [rounded(p) for p in item.get('points') or ()], 'width': item.get('width'),
                            'layer': item.get('layer'), 'net': item.get('net', '')}
                           for item in (section.get('tracks') or ())][:300],
                'vias': [{'at': rounded(item['at']), 'diameter': item.get('diameter'), 'net': item.get('net', '')}
                         for item in (section.get('vias') or ()) if item.get('at')][:200],
                'pads': pads[:300]}

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
        document = bounded(document, self.reference_roots)
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

    @staticmethod
    def _footprint_file(root, footprint):
        """A footprint's file in a library copy; a wrong name says so (review of #240)."""
        if not isinstance(footprint, str) or footprint.count('/') != 1:
            raise KiCadError('arguments_unusable', {'detail': 'footprint is LIBRARY/NAME, e.g. FireFli_Misc/7488912455'})
        library, name = footprint.split('/')
        found = [path for path in (root / 'project').rglob(library + '.pretty') if path.is_dir()]
        if len(found) != 1 or not (found[0] / (name + '.kicad_mod')).is_file():
            raise KiCadError('arguments_unusable', {'detail': f'no footprint {footprint} in this library copy'})
        return found[0] / (name + '.kicad_mod')

    def _footprint_board(self, root, footprint):
        """A library footprint placed alone on a board, as part FP, for measuring."""
        source = self._footprint_file(root, footprint)
        library, name = footprint.split('/')
        found = [source.parent]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:24]
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
        document = bounded(document, self.reference_roots)
        try:
            page, section = section_box(arguments)
            words = extract_words(document, page, scratch_root=self.work_root / 'sections',
                                  extractor=self.engine_paths.get('pdftotext') or Path('/nonexistent'),
                                  execute=self.execute_page)
        except (PageError, LayoutCheckError) as error:
            raise KiCadError(error.code, {'detail': error.detail}) from error
        numbers = read_numbers(words, section)
        if not numbers and self.build_program is not None:
            # A drawing that is a picture holds no text: its numbers are read
            # by text recognition and marked so (14 Nov: TL3340, 830053099).
            from alx.providers.picture_text import engine, picture_words
            try:
                recognizer = engine(self.work_root / 'engines', self.build_program)
                key = hashlib.sha256(json.dumps([document_digest(document), page, list(section)]).encode()).hexdigest()
                words = picture_words(document, page, section, scratch_root=self.work_root / 'sections',
                                      renderer=self.engine_paths.get('pdftoppm') or Path('/nonexistent'),
                                      recognizer=recognizer, execute=self.execute_page,
                                      cache=self.work_root / 'picture-words' / (key[:32] + '.json'))
            except LayoutCheckError as error:
                raise KiCadError(error.code, {'detail': error.detail}) from error
            numbers = [dict(number, read_from='picture') for number in read_numbers(words, section)]
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
                                                   rename_nets)
        from alx.providers.kicad_index import netlist_nets
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            project = root / 'project'
            schematic = (project / meta['board']).with_suffix('.kicad_sch')
            if not schematic.is_file():
                raise KiCadError('unsupported_project', {'detail': 'no schematic beside the board'})

            def netlist():
                return netlist_nets(self._netlist(project, meta['board'], root / 'artifacts'))
            before = netlist()
            # One kept change, gone back over with revert_to like any edit (18 Nov review: it had its
            # own copy-and-restore and left no history).
            with self._change(root, meta) as copy_change:
                keep = frozenset()
                for _ in range(3):
                    try:
                        result = make_hierarchical(project)
                        # Then the sheets' connections drawn as Altium showed them:
                        # pins on the blocks, wires between (Friedl, 2026-11-13).
                        pins = connect_with_sheet_pins(project, keep)
                    except HierarchyError as error:
                        raise KiCadError(error.code, {'detail': error.detail}) from error
                    after = netlist() if result['converted'] or pins['connected'] else before
                    differing = sorted({before.get(k, '') for k in set(before) - set(after)}
                                       | {after.get(k, '') for k in set(after) - set(before)})
                    # A signal left to a bus that does not really carry it comes apart:
                    # it keeps its own wire, and the conversion is done again (16 Nov).
                    split = {name.rsplit('/', 1)[-1] for name in differing}
                    loose = split & set(pins.get('carried_by_buses') or ())
                    if not differing or not loose or split - loose:
                        break
                    keep |= loose
                    copy_change.reset()
                result = dict(result, connected=pins['connected'], carried_by_buses=pins.get('carried_by_buses', []),
                              reason=result['reason'] or pins['reason'])
                if 'top_sheet_crossings' in pins:
                    result['top_sheet_crossings'] = pins['top_sheet_crossings']
                # Every connection must be the same. Only a net now joined on the
                # top sheet by a plain net label may take KiCad's hierarchical
                # name, and the board, its rules and net classes follow it.
                if differing:
                    raise KiCadError('verification_failed', {'detail': 'the netlist changed; the copy is back '
                                                             'as it was', 'nets_differing': differing[:20]})
                renames = {before[k]: after[k] for k in before if before[k] != after[k]}
                # A signal a bus carries takes KiCad's bus-member name (/I2C.SDA), 16 Nov.
                carried = set(pins.get('carried_by_buses') or ())
                unexpected = sorted(old for old, new in renames.items()
                                    if not ((new == '/' + old and pins['connected'])
                                            or (old in carried and re.fullmatch(r'/[^/.]+\.' + re.escape(old), new))))
                if unexpected:
                    raise KiCadError('verification_failed', {'detail': 'a net name changed other than to its '
                                                             'hierarchical name; the copy is back as it was',
                                                             'nets_differing': unexpected[:20]})
                result['renamed_nets'] = renames
                result['rename_counts'] = rename_nets(project, renames)
                # What the joined design now shows: the board against its whole
                # schematic, and the ERC of every sheet (review of #239). Inside
                # the undo, so a failed check never leaves the change unreported.
                checked = self._drc(root, meta)
                erc = self._erc(root, meta)
            return dict(result, nets=len(after), netlist_identical=True,
                        schematic_parity=checked['schematic_parity'], erc=erc,
                        work_copy_id=arguments['work_copy_id'], work_copy_revision=self._revision(root))

    def check_layout(self, arguments):
        """Her copy, or his board, measured against the manufacturer's drawing (2026-11-13)."""
        from alx.providers.layout_check import MAX_NUMBERS, compare, label, measure_board, measurement
        # The state is taken before anything is read or built from the board,
        # so a change while it is measured is always seen (review of #240).
        state = self._board_state(arguments)
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
        reason = ('' if printed else 'no number could be read in this section, as text or from a picture: '
                  'a section that misses the drawing; nothing was checked against it')
        if any(row.get('read_from') == 'picture' for row in rows):
            reason = ('the drawing is a picture: its numbers were read by text recognition; look at the '
                      'section and say if any number is misread')
        result = {'check_id': uuid.uuid4().hex, 'work_copy_id': arguments.get('work_copy_id') or '',
                  'document': document.name, 'document_path': str(document),
                  'page': page, 'section': list(section), 'part': part, 'rf': bool(stored.get('rf')),
                  # Which library footprint was measured, so the screen never shows
                  # this check beside another one (review of D-081).
                  'footprint': arguments.get('footprint') or '',
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
                return sha(self._footprint_file(root, arguments.get('footprint')))
            return self._revision(root)
        board, _ = self._layout_target(arguments)
        return sha(board)

    @staticmethod
    def _readings_digest(stored):
        # MEASURING names how readings are measured: a confirmation given
        # before a change to that (pads sharing a number measured as one,
        # 14 Nov) is not carried over to a reading that now means more.
        return hashlib.sha256(json.dumps([stored.get('entries'), bool(stored.get('rf')), MEASURING],
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

    def fetch_datasheet(self, arguments):
        """A part's datasheet PDF by part number, into her datasheets folder (2026-11-13)."""
        from alx.providers.datasheet_fetch import DatasheetError, fetch_datasheet
        if self.datasheet_sources is None or self.datasheet_folder is None:
            raise KiCadError('engine_unavailable', {'detail': 'no datasheet source is configured'})
        client, nexar, digikey, fetcher = self.datasheet_sources()
        try:
            with client:
                return fetch_datasheet(arguments.get('part_number', ''), arguments.get('manufacturer') or '',
                                       self.datasheet_folder, client=client, nexar=nexar, digikey=digikey,
                                       download=fetcher.fetch_pdf)
        except DatasheetError as error:
            recovery = {'detail': error.detail}
            if error.candidates:
                # Several parts answer to this number: each with maker and package, for her choice.
                recovery['candidates'] = error.candidates
            raise KiCadError({'not_found': 'document_unavailable', 'ambiguous_part': 'arguments_unusable'}
                             .get(error.code, error.code), recovery) from error
        finally:
            # Both connections are closed, whatever happened (review of #243).
            fetcher.close()

    def _storage_root(self):
        """AL/X's storage (ALX_Storage), from the design folder that is its Projects."""
        for root in self.design_roots:
            if root.name == 'Projects' and root.parent.name == 'ALX_Storage':
                return root.parent
        return None

    def _reference_location(self, text):
        """Where a read points, the way models name it (16 Nov: 30 refused reads guessed
        '~/ALX_Storage/...', 'P0020/datasheets' or AL/X's own docs)."""
        given = Path(str(text).strip())
        parts = given.parts
        storage = self._storage_root()
        for alias in (('~', 'ALX_Storage'), (Path.home() / 'ALX_Storage').parts):
            if storage is not None and parts[:len(alias)] == alias:
                return storage.joinpath(*parts[len(alias):])
        if parts and not given.is_absolute() and parts[0] != '~':
            head, rest = parts[0], parts[1:]
            if len(head) >= 4 and head[0].isascii() and head[0].isalpha() and head[1:].isascii() and head[1:].isdigit():
                found = [path for root in self.design_roots for path in root.glob('*')
                         if path.is_dir() and path.name.split('-', 1)[0].lower() == head.lower()]
                if len(found) == 1:
                    return found[0].joinpath(*rest)
            raise KiCadError('path_denied', {'detail': (
                'give a full path; a relative path is not read. AL/X\'s own documents are not readable here: '
                'your brief and instructions carry the job. ' + self._where_things_are())})
        return Path(text).expanduser()

    def _where_things_are(self):
        storage = self._storage_root()
        return ('Projects are found by number: kicad check or inspect with project_dir P0020. A datasheet: '
                'kicad fetch_datasheet gives its path. Design folders: %s.%s' % (
                    ', '.join(str(root) for root in self.design_roots),
                    ' AL/X storage: %s.' % storage if storage else ''))

    def read_reference(self, arguments):
        if Path(str(arguments.get('path', ''))).name.startswith(('alx-native-', 'alx-claude-')):
            # The model session's own working folder (19 Nov 2026: read twice by the board agent).
            raise KiCadError('path_denied', {'detail': 'that is your session\'s own scratch folder; nothing of '
                                                       'his design is there. ' + self._where_things_are()})
        location = self._reference_location(arguments['path'])
        placeholder = location.with_name('.' + location.name + '.icloud')
        if not location.exists() and not placeholder.is_file():
            # A refused place stays refused; only an allowed one that is missing gets the map.
            from alx.providers.reference_file import ReferenceError as _Refused, reference_path
            try:
                reference_path(location)
            except _Refused as error:
                raise KiCadError(str(error)) from error
            raise KiCadError('unavailable', {'detail': 'no such file or folder: %s. %s' % (
                location, self._where_things_are())})
        arguments = dict(arguments, path=str(location))
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
        presentation = presentation_edit_scope.get()
        if presentation is not None:
            if plain(arguments.get('intent')) != []:
                raise KiCadError('arguments_unusable', {'detail': 'presentation edits require intent: []'})
            if arguments['work_copy_id'] != presentation.work_copy_id or arguments.get('revert_to') is not None:
                raise KiCadError('arguments_unusable', {'detail': 'presentation edits must redraw the job work copy'})
        with self._locked(arguments['work_copy_id']):
            if arguments.get('revert_to') is not None:
                if set(arguments) - {'work_copy_id', 'revert_to'}:
                    raise KiCadError('arguments_unusable', {'detail': 'revert_to goes alone'})
                return self._revert(arguments)
            if 'dry_run' in arguments:
                # Edit directly (26 Nov: 61 of 105 edits in one job were trial runs, each then done
                # again for real). An edit his order or the gate refuses is undone anyway, with why.
                raise KiCadError('arguments_unusable', {'detail': 'there are no trial runs: edit directly. '
                                                        'A refused edit is undone with the reason; revert_to undoes a kept one'})
            script = arguments.get('script')
            try:
                declared = validate_postconditions(plain(arguments.get('intent', [])))
            except ChangeError as error:
                raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
            label_exceptions = [item for item in declared if 'label_net' in item]
            orientation_exceptions = [item for item in declared if 'orientation' in item]
            postconditions = [item for item in declared if 'op' in item]
            sets = [item for item in postconditions if item['op'] == 'set']
            if sets and not arguments.get('work_copy_revision'):
                raise KiCadError('arguments_unusable', {'detail': 'set needs the revision returned by select'})
            if not script or len(script) > 128000 or self.execute_python is None:
                raise KiCadError('arguments_unusable')
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            # The revision she read (select's) is the one she changes (18 Nov review).
            # Say what it is now, so one call corrects it (25 Nov: four refusals in a row, no detail).
            if (arguments.get('work_copy_revision') is not None
                    and self._revision(root, meta) != arguments['work_copy_revision']):
                raise KiCadError('work_copy_changed', {'detail': 'the copy is now at revision %s, not %s: look at '
                                                       'it again (or take_edits if Friedl saved it in KiCad), then '
                                                       'pass that revision' % (self._revision(root, meta),
                                                                               arguments['work_copy_revision'])})
            if presentation is not None and self._revision(root, meta) != presentation.work_copy_revision:
                raise KiCadError('work_copy_changed', {'detail': 'the job copy moved on to revision %s since the '
                                                       'presentation pass started' % self._revision(root, meta)})
            project = root / 'project'
            footprints = self._library_parts(arguments.get('footprints') or (), project, 'footprints')
            # From his library even where a sheet already keeps a copy: that copy may be bare
            # (16 Nov: the stored 100 pF part had no MPN or LCSC). A copy the library cannot
            # give (an import's own symbols) stays as the sheet keeps it.
            symbols = {}
            for name in arguments.get('symbols') or ():
                try:
                    symbols.update(self._library_parts([name], project, 'symbols'))
                except KiCadError:
                    if not self._sheet_has_symbol(project, name):
                        raise
            library = bool(meta.get('library'))
            if presentation is not None and library:
                raise KiCadError('arguments_unusable', {'detail': 'presentation needs a board work copy'})
            # A library copy has no board: the script's argument is its folder.
            board = project / ('.library' if library else meta['board'])
            empty = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
            script_path = root / ('edit-' + uuid.uuid4().hex + '.py')
            stage = 'before_drc'
            evidence, copy_change = None, None
            try:
                with self._change(root, meta) as copy_change:
                    snapshot, original = copy_change.snapshot, copy_change.original
                    before, prior_drc = (empty, []) if library else self._board_drc(root, meta)
                    facts_before = board_facts('(kicad_pcb)' if library else board.read_text())
                    steps_before = None if library else fingerprints(project, meta['board'])
                    # Tested helpers first, then her script compiled on its own, so
                    # its own first lines (a __future__ import) and line numbers
                    # stay as she wrote them (2026-11-12).
                    model = None if library else self._netlist(project, meta['board'], root / 'artifacts')
                    sync = self._schematic_sync(project, meta, model) if model is not None else {}
                    blocks = {} if library else {
                        name: path.read_text() for name, (path, _) in self._design_blocks().items()
                    }
                    script_path.write_text(HELPERS + SCHEMATIC_HELPERS + BOARD_HELPERS + RF_HELPERS
                                           + 'SCHEMATIC_SYNC = %a\n' % sync
                                           + '_DESIGN_BLOCKS.update(%a)\n' % blocks
                                           + '_LIBRARY_FOOTPRINTS.update(%a)\n' % footprints
                                           + '_LIBRARY_SYMBOLS.update(%a)\n' % symbols
                                           + script_runner(script))
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
                    after, signatures = (empty, []) if library else self._board_drc(root, meta)
                    steps = {}
                    if not library:
                        # Friedl's order (D-082): an edit that gets ahead of his
                        # approvals is undone, and says which step he must see.
                        steps_after = fingerprints(project, meta['board'])
                        original_steps = self._original_steps(root, meta)
                        refusal = edit_refusal(steps_before, steps_after, original_steps, meta.get('approvals', {}))
                        steps = step_status(original_steps, meta.get('approvals', {}), steps_after)
                    current = design_inventory(project)
                    facts_after = board_facts('(kicad_pcb)' if library else board.read_text())
                    changed = sorted(name for name in design_inventory(snapshot).keys() | current.keys()
                                     if original.get(name) != current.get(name))
                    if presentation is not None:
                        outside = [name for name in changed
                                   if not ((name.endswith('.kicad_sch') and name in original and name in current)
                                           or (name.endswith('.kicad_sch' + GRID_SUFFIX)
                                               and name[:-len(GRID_SUFFIX)] in original))]
                        if outside:
                            raise KiCadError('change_unmet', {'unmet': [
                                'presentation may only redraw existing schematic sheets: ' + ', '.join(outside)]})
                    report = {'files_changed': changed[:200], 'files_truncated': len(changed) > 200,
                              'board': structural_diff(facts_before, facts_after),
                              'label_exceptions': label_exceptions,
                              'orientation_exceptions': orientation_exceptions}
                    if library and (declared or any(n.endswith(('.kicad_pcb', '.kicad_sch')) for n in changed)):
                        raise KiCadError('arguments_unusable', {'detail': 'check a board work copy to edit drawings'})
                    if not library and (changed or declared):
                        stage = 'edit_gate'
                        report.update(self._edit_gate(root, meta, snapshot, changed, model, postconditions,
                                                      sets, prior_drc, signatures, label_exceptions,
                                                      orientation_exceptions, presentation=presentation is not None))
                        if (any(name.endswith(('.kicad_sch', '.kicad_pcb')) for name in changed)
                                and 'intent' not in arguments):
                            report['unmet'].append('intent is required for a drawing edit; [] for unchanged connectivity')
                        if report['unmet']:
                            raise KiCadError('change_unmet', {k: report[k] for k in ('unmet', 'gate')})
                    if not library and changed:
                        report['compared_to_original'] = self._compared(root, meta, signatures)
                        report['errors_to_show'] = errors_to_show(arguments['work_copy_id'], signatures)
                        if refusal:
                            raise KiCadError('step_not_approved', refusal)
                    # Friedl's taper rule on the edited board, so she sees a step
                    # before she shows anything (2026-11-12, finished 2026-11-13).
                    # A script that only read changed nothing to check (15 Nov: 26
                    # of the agent's 31 edits only read, each paying for every check).
                    report['layout_rules'] = ({'findings': [], 'omitted': 0, 'not_checked': 'a library has no board'}
                                              if library else
                                              {'findings': [], 'omitted': 0, 'not_checked': 'nothing changed'}
                                              if not changed else self._layout_rules(board))
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
                    truncated = (len(diffs) < len(changed) or any(d['truncated'] for d in diffs))
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
                    if not library and changed:
                        from alx.providers.kicad_drawing import drawings
                        report['drawings'] = drawings(self, snapshot, project, changed,
                                                      steps_before['routing'] != steps_after['routing'])
                    if evidence:
                        # The snapshot becomes the evidence's before copy.
                        copy_change.keep_snapshot_at = evidence / 'before'
                    result = {'work_copy_id': arguments['work_copy_id'],
                              'stdout': stdout[:16000], 'stderr': stderr[:16000],
                              'change_report': report, 'before': before, 'after': after, 'steps': steps}
                    if not library:
                        # Seen as it happens, not first at write-back: parts new to his project that are
                        # not FireFli library parts, which write_back will refuse (19 Nov 2026).
                        try:
                            original = set(_non_library_parts(Path(meta['source']).parent
                                                              if Path(meta['source']).is_file()
                                                              else Path(meta['source'])))
                        except (KeyError, OSError):
                            original = set()
                        new = sorted(set(_non_library_parts(project)) - original)
                        if new:
                            result['not_library_parts'] = {
                                'parts': ['%s (%s)' % item for item in new[:30]],
                                'detail': 'write_back refuses these: use a FireFli part (find_part), or add_part '
                                          'from the datasheet first'}
                return dict(result, work_copy_revision=copy_change.revision)
            except BaseException as error:
                restored = getattr(error, 'rollback_restored', copy_change is None)
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

    @staticmethod
    def _covered_sheets(project, board, model):
        """Resolve exported instance UUID paths to files, including nested/reused sheets."""
        if model is None:
            return set()
        top = (project / board).with_suffix('.kicad_sch')
        by_instance = {'': top}
        children = {}
        for instance in sorted(model['sheet_paths'], key=lambda path: path.count('/')):
            if not instance:
                continue
            parent, _, uid = instance.rpartition('/')
            path = by_instance.get(parent)
            if path is None:
                continue
            if path not in children:
                children[path] = {}
                for node in sexpr(path.read_text()).named('sheet'):
                    prop = next((p for p in node.named('property')
                                 if p.children[1].lower() == 'sheetfile'), None)
                    if prop is not None and node.named('uuid'):
                        children[path][node.named('uuid')[0].children[1]] = prop.children[2]
            if uid in children[path]:
                child = (path.parent / children[path][uid]).resolve()
                if not child.is_relative_to(project.resolve()) or not child.is_file():
                    raise KiCadError('unsupported_project', {'detail': f'invalid child of {path.name}: {uid}'})
                by_instance[instance] = child
        # KiCad 10 also exports multiple independent top sheets from project settings.
        settings = top.with_suffix('.kicad_pro')
        roots = json.loads(settings.read_text()).get('schematic', {}).get('top_level_sheets', []) if settings.exists() else []
        represented = set(model['sheet_paths'])
        files = {path.relative_to(project).as_posix() for path in by_instance.values()}
        files.update(entry['filename'] for entry in roots if entry.get('uuid') in represented)
        return files

    @staticmethod
    def _schematic_references(project, board):
        """Files referenced by a sheet symbol, primary schematic or project settings."""
        top = (project / board).with_suffix('.kicad_sch')
        found = {top.resolve()}
        settings = top.with_suffix('.kicad_pro')
        if settings.is_file():
            roots = json.loads(settings.read_text()).get('schematic', {}).get('top_level_sheets', [])
            found.update((top.parent / item['filename']).resolve() for item in roots)
        for name in design_inventory(project):
            if not name.endswith('.kicad_sch'):
                continue
            path = project / name
            content = path.read_text()
            if not content.strip():
                continue
            for node in sexpr(content).named('sheet'):
                for prop in node.named('property'):
                    if prop.children[1].lower() == 'sheetfile':
                        found.add((path.parent / prop.children[2]).resolve())
        return found

    def _edit_gate(self, root, meta, snapshot, changed, model, postconditions, sets, prior_drc, drc,
                   label_exceptions=(), orientation_exceptions=(), *, presentation=False):
        from alx.providers.kicad_index import board_index
        project, board = root / 'project', meta['board']
        sheets = [name for name in changed if name.endswith('.kicad_sch')]
        removed = [name for name in sheets if not (project / name).is_file()]
        still_referenced = set()
        if removed:
            new_refs = self._schematic_references(project, board)
            still_referenced = {name for name in removed if (project / name).resolve() in new_refs}
            sheets = [name for name in sheets if name not in removed or name in still_referenced]
        board_items = [item for item in postconditions if item.get('side') == 'board']
        sch_items = [item for item in postconditions if item.get('side', 'schematic') == 'schematic']
        gate = {'style': [], 'erc': [], 'drc': [], 'style_warnings': [], 'label_joins': [], 'orientation': []}
        problems = []
        report = {'gate': gate, 'unmet': problems}
        if any(n.endswith('.kicad_pcb') and n != board for n in changed):
            problems.append('cannot check an additional board against this work copy')
        problems.extend(f'{name} is still referenced by the schematic hierarchy' for name in sorted(still_referenced))
        electrical = sheets or sch_items or any(n.endswith(('.kicad_pro', '.kicad_sym', 'sym-lib-table')) for n in changed)
        after = model
        if electrical:
            after = self._netlist(project, board, root / 'artifacts')
            problems.extend(unmet(sch_items, design_state(model), design_state(after)))
            if presentation and model is not None and after is not None:
                before_parts = {c['ref']: c for c in model['components']}
                after_parts = {c['ref']: c for c in after['components']}
                for ref in sorted(before_parts.keys() & after_parts.keys()):
                    for field in ('value', 'footprint', 'fields'):
                        if before_parts[ref][field] != after_parts[ref][field]:
                            problems.append(f'presentation changed {ref} {field}')
            if sheets and after is None:
                problems.append('cannot verify changed sheets without a root schematic')
            if any(name != Path(board).with_suffix('.kicad_sch').as_posix() for name in sheets):
                for folder, reading in ((snapshot, model), (project, after)):
                    covered = self._covered_sheets(folder, board, reading)
                    problems.extend(f'{name} is outside the exported schematic hierarchy'
                                    for name in sheets if (folder / name).is_file() and name not in covered)
            for name in sheets:
                for kind, x, y, pins in sorted(violations(project / name) - violations(snapshot / name)):
                    issue = f'{name} ({x}, {y}): {kind}: {", ".join(pins)}'
                    gate['style'].append(issue)
                    problems.append(issue)
                old = orientation_violations(snapshot / name)
                allowed_refs = {item['orientation'] for item in orientation_exceptions}
                for key, issue in sorted(orientation_violations(project / name).items()):
                    if key in old or issue['ref'] in allowed_refs:
                        continue
                    gate['orientation'].append(dict(issue, sheet=name))
                    message = f'{name} {issue["at"]}: {issue["ref"]}: {issue["rule"]}'
                    gate['style'].append(message)
                    problems.append(message)
        warn_sheets = set(sheets) | {name[:-len(GRID_SUFFIX)] for name in changed
                                     if name.endswith('.kicad_sch' + GRID_SUFFIX)}
        if warn_sheets or after != model:
            allowed = {item['label_net'] for item in label_exceptions}
            covered = self._covered_sheets(project, board, after)
            for name in sorted(covered if sheets or after != model else covered & warn_sheets):
                old = label_violations(snapshot / name, model)
                for key, issue in label_violations(project / name, after).items():
                    prior = old.get(key)
                    if (prior and (not prior['warning'] or issue['warning'])
                            and issue['labels'] == prior['labels']
                            and len(issue['positions']) <= len(prior['positions'])):
                        continue
                    if allowed & {issue['net'], *issue['labels']}:
                        continue
                    gate['label_joins'].append(dict(issue, sheet=name))
                    message = (f'{name}: label-joined pins: {issue["net"]} '
                               f'{", ".join(issue["pins"])} at {issue["positions"]}')
                    if issue['warning']:
                        gate['style_warnings'].append(message + ' (block boundaries unavailable)')
                    else:
                        message += f' inside block {issue["block"]}'
                        gate['style'].append(message)
                        problems.append(message)
        for name in sorted(warn_sheets):
            old = placement_warnings(snapshot / name)
            gate['style_warnings'].extend(
                f'{name}: {warning}' for warning in sorted(placement_warnings(project / name) - old))
        if electrical or board in changed:
            before_erc, after_erc = [], []
            self._erc(root, meta, project=snapshot, findings=before_erc)
            erc = self._erc(root, meta, against_original=True, findings=after_erc, model=after)
            if electrical:
                report['erc'] = erc
            gate['erc'] = self._new_findings(before_erc, after_erc, models=(model, after))
        if board in changed or board_items:
            old_index = board_index((snapshot / board).read_text())
            new_index = board_index((project / board).read_text())
            problems.extend(unmet(board_items, board_state(old_index), board_state(new_index)))
            if sets:
                for item in sets:
                    available = {o['id'] for o in old_index['objects'] if o['kind'] == item['kind']}
                    problems.extend(f'{uid}: no {item["kind"]} in selected revision'
                                    for uid in item['ids'] if uid not in available)
                report['set'], wrong = set_check(sets, old_index, new_index)
                problems.extend(wrong)
        gate['drc'] = self._new_findings(prior_drc, drc)
        # A new connection without copper is the ratsnest a placement leaves for routing: reported,
        # never blocking, when the edit added no copper (25 Nov: synced and placed parts showed 29
        # unrouted connections, and routing waits on the placement, so it could never be saved).
        unrouted_ok = board in changed and not routing_added(snapshot / board, project / board)
        for checker in ('erc', 'drc'):
            problems.extend(f'{checker.upper()} {f["type"]}: {f["description"]} {f["items"]}'
                            for f in gate[checker] if f['severity'] == 'error'
                            and not (checker == 'drc' and unrouted_ok and f['type'] == 'unconnected_items'))
        return report

    @staticmethod
    def _erc_context(model):
        return design_state(model)[1], {net['name'] for net in model['nets']} if model else set()

    @staticmethod
    def _erc_key(finding, context):
        by_pin, net_names = context
        descriptions = [item.get('description', '') for item in finding['items']]
        # ERC item descriptions follow KiCad's report format. Strip display details
        # such as pin names, sheet paths and marker locations from the identity.
        pins = [re.match(r'^Symbol (\S+)(?: (?:Hidden pin|Pin) (\S+))?(?= \[|$| Hidden pin)', description)
                for description in descriptions]
        if finding['type'] == 'power_pin_not_driven' and len(descriptions) == 1:
            # KiCad can pick a different representative input on the same undriven net.
            power = re.match(r'^Symbol #PWR\S* Hidden pin\s+\[([^,]+), Power input,', descriptions[0])
            net = power[1] if power and power[1] in net_names else None
            if pins[0] and pins[0][2] and not net:
                net = by_pin.get(f'{pins[0][1]}.{pins[0][2]}')
            if net:
                return json.dumps([finding['type'], finding['description'], net])
        return json.dumps([finding['type'], finding['description'],
                           sorted((pin[1], pin[2] or '') for pin in pins if pin)])

    @classmethod
    def _new_findings(cls, before, after, models=None):
        """Subtract occurrences, keeping complete evidence for each new finding."""
        def keys(findings, model):
            if models is not None:
                context = cls._erc_context(model)
                return [cls._erc_key(f, context) for f in findings]
            return [json.dumps([f.get('sheet'), f['type'], f['severity'], f['description'],
                                sorted(f['items'], key=str), f.get('ids') or f.get('at')], sort_keys=True)
                    for f in findings]

        old_model, new_model = models if models is not None else (None, None)
        old = Counter(keys(before, old_model))
        new = []
        for signature, finding in zip(keys(after, new_model), after):
            if old[signature]:
                old[signature] -= 1
            else:
                new.append(finding)
        return new

    @contextmanager
    def _change(self, root, meta, *, record=True):
        """The one way her work copy changes (18 Nov review: edit, hierarchy conversion, taking in
        his saved edits and revert each had their own copy-and-restore).

        Before: a verified snapshot of the copy and a durable pending taint. The caller changes
        `project` inside the block and may set `change.meta` (the metadata to keep) and
        `change.keep_snapshot_at` (a path the snapshot moves to as evidence). After: the change
        is kept in history (unless record is False) and the taint cleared, last. On any failure the
        copy is put back from the snapshot and checked; if that cannot be proven, the taint stays
        and so does the snapshot. The failure carries `rollback_restored`.
        """
        project = root / 'project'
        original = inventory(project)
        revision_before = self._revision(root, meta)
        snapshot = root / ('snapshot-' + uuid.uuid4().hex)
        shutil.copytree(project, snapshot)
        if inventory(snapshot) != original:
            shutil.rmtree(snapshot, ignore_errors=True)
            raise KiCadError('verification_failed')
        self._save_metadata(root, dict(meta, taint=uuid.uuid4().hex))
        def reset():
            """Back to how the copy was when the change began, inside it (a retry starts again)."""
            shutil.rmtree(project)
            shutil.copytree(snapshot, project)
        change = SimpleNamespace(project=project, snapshot=snapshot, original=original, meta=meta,
                                 keep_snapshot_at=None, changed=[], revision=revision_before,
                                 on_commit=[], on_rollback=[], reset=reset)
        settled, pending = False, None
        try:
            yield change
            before_files = design_inventory(snapshot)
            current = design_inventory(project)
            change.changed = sorted(name for name in before_files.keys() | current.keys()
                                    if before_files.get(name) != current.get(name))
            change.revision = self._revision(root, change.meta)
            if change.changed and record:
                pending = self._record_history(root, snapshot, original, change.changed, revision_before,
                                               change.revision)
                # Published (one rename) while the taint still stands: if anything after fails, the
                # entry is taken back with the change, so a settled edit always has its undo entry and
                # an undone one never does (19 Nov recheck).
                pending = self._publish_history(root, pending)
            if change.keep_snapshot_at is not None:
                snapshot.rename(change.keep_snapshot_at)
                change.snapshot = change.keep_snapshot_at
            # The pending taint is cleared last; nothing after it can fail the change.
            self._save_metadata(root, change.meta)
            settled = True
            # Only tidying follows: old entries beyond the limit, and what a revert retired.
            with suppress(OSError):
                for old in self._history_entries(root)[:-self.HISTORY_KEPT]:
                    shutil.rmtree(old, ignore_errors=True)
                for action in change.on_commit:
                    action()
        except BaseException as error:
            restored = False
            if pending is not None:
                shutil.rmtree(pending, ignore_errors=True)
            history_back = pending is None or not pending.exists()
            try:
                for action in reversed(change.on_rollback):
                    action()
                # Never follow a link a script made while restoring.
                if project.is_symlink():
                    project.unlink()
                elif project.exists():
                    shutil.rmtree(project)
                shutil.copytree(change.snapshot, project)
                # Restored means the files and the undo history: an entry left for an undone edit is not.
                restored = inventory(project) == original and history_back
                # A session that could not be stopped may still write: unproven.
                if getattr(error, 'details', {}).get('reason_code') == 'session_unstoppable':
                    restored = False
                if restored:
                    self._save_metadata(root, meta)
            except (OSError, KiCadError):
                restored = False
            settled = restored
            error.rollback_restored = restored
            raise
        finally:
            # A settled change never keeps its snapshot, except as evidence; unproven recovery does.
            if settled and change.snapshot == snapshot:
                shutil.rmtree(snapshot, ignore_errors=True)

    HISTORY_KEPT = 60

    @staticmethod
    def _history_entries(root):
        """The kept edits in order, never one still being written (a .pending entry)."""
        history = root / 'history'
        return sorted(path for path in history.iterdir()
                      if not path.name.startswith('.')) if history.is_dir() else []

    def _record_history(self, root, snapshot, original, changed, before, after):
        """Write the files an edit changed, as they were before it, to a pending entry; it becomes
        a kept edit only when the change is settled (18 Nov review: an entry written before the
        taint was cleared stayed for a change that was then undone, and pruned a good one)."""
        history = root / 'history'
        history.mkdir(exist_ok=True)
        pending = history / ('.pending-' + uuid.uuid4().hex)
        try:
            (pending / 'files').mkdir(parents=True)
            created = [name for name in changed if name not in original]
            for name in changed:
                if name in original:
                    (pending / 'files' / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snapshot / name, pending / 'files' / name)
            (pending / 'manifest.json').write_text(json.dumps(
                {'before': before, 'after': after, 'changed': changed, 'created': created}))
        except BaseException:
            shutil.rmtree(pending, ignore_errors=True)
            raise
        return pending

    def _publish_history(self, root, pending):
        """A change's pending entry becomes the newest kept edit, in one rename; returns its path,
        so a change that then fails takes it back."""
        entries = self._history_entries(root)
        number = int(entries[-1].name.split('-', 1)[0]) + 1 if entries else 1
        entry = root / 'history' / ('%05d-%s' % (number, uuid.uuid4().hex[:8]))
        pending.rename(entry)
        return entry

    def _revert(self, arguments):
        """Put the copy back as it was at an earlier revision of its kept edits.

        15 Nov: a bad edit had nothing to go back with; the agent stopped,
        blocked, on a copy it could not repair.
        """
        root, meta = self._load(arguments['work_copy_id'])
        if meta.get('taint'):
            raise KiCadError('work_copy_tainted')
        if (arguments.get('work_copy_revision') is not None
                and self._revision(root, meta) != arguments['work_copy_revision']):
            raise KiCadError('work_copy_changed')
        entries = [(path, json.loads((path / 'manifest.json').read_text())) for path in self._history_entries(root)]
        current = self._revision(root, meta)
        if not entries or entries[-1][1]['after'] != current:
            raise KiCadError('arguments_unusable', {'detail': 'this copy has no kept edits to go back over'})
        target = str(arguments['revert_to'])
        if target in ('start', 'first'):
            target = entries[0][1]['before']
        index = next((i for i in range(len(entries) - 1, -1, -1)
                      if len(target) >= 8 and entries[i][1]['before'].startswith(target)), None)
        if index is None:
            raise KiCadError('arguments_unusable', {
                'detail': 'revert_to is one of these revisions (newest first) or start: '
                          + ', '.join(m['before'][:12] for _, m in reversed(entries[-10:]))})
        project = root / 'project'
        library = bool(meta.get('library'))
        board_text = (lambda: '(kicad_pcb)' if library else (project / meta['board']).read_text())
        facts_before = board_facts(board_text())
        # Inside one change: if putting the files back or reporting on them fails, the copy is as
        # it was and so is its history (18 Nov review: history went before the reports finished).
        with self._change(root, meta, record=False) as copy_change:
            # The edits gone back over leave history with the change: set aside now, put back if
            # it fails, removed only once it is settled (18 Nov review).
            retired = root / 'history' / ('.retired-' + uuid.uuid4().hex)
            retired.mkdir()
            # Registered before anything moves, so a failure part-way puts back what did move.
            # Rollback runs these last first: put the entries back, then drop the empty holder.
            copy_change.on_rollback.append(lambda: shutil.rmtree(retired, ignore_errors=True))
            copy_change.on_rollback.append(lambda: [moved.rename(root / 'history' / moved.name)
                                                    for moved in sorted(retired.iterdir())])
            for path, _ in entries[index:]:
                path.rename(retired / path.name)
            copy_change.on_commit.append(lambda: shutil.rmtree(retired, ignore_errors=True))
            entries = [(retired / path.name, manifest) if i >= index else (path, manifest)
                       for i, (path, manifest) in enumerate(entries)]
            changed = set()
            for path, manifest in reversed(entries[index:]):
                for name in manifest['changed']:
                    changed.add(name)
                    if name in manifest['created']:
                        (project / name).unlink(missing_ok=True)
                    else:
                        (project / name).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path / 'files' / name, project / name)
            revision = self._revision(root, meta)
            if revision != entries[index][1]['before']:
                raise KiCadError('verification_failed', {'detail': 'the copy did not come back to that revision'})
            report = {'files_changed': sorted(changed), 'files_truncated': False, 'file_diffs': [],
                      'diffs_truncated': False, 'board': structural_diff(facts_before, board_facts(board_text())),
                      'reverted_edits': len(entries) - index}
            steps, totals = {}, {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
            if not library:
                steps = step_status(self._original_steps(root, meta), meta.get('approvals', {}),
                                    fingerprints(project, meta['board']))
                totals = self._board_drc(root, meta)[0]
                if any(name.endswith('.kicad_sch') for name in changed):
                    report['erc'] = self._erc(root, meta, against_original=True)
        return {'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': revision, 'stdout': '',
                'stderr': '', 'change_report': report, 'before': totals, 'after': totals, 'steps': steps}

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
            # The files are of the revision she names (18 Nov review: the copy could change
            # between the request and the export).
            if self._revision(root, meta) != arguments.get('work_copy_revision'):
                raise KiCadError('work_copy_changed')
            staging = root / 'artifacts' / ('manufacturing-' + uuid.uuid4().hex)
            staging.mkdir(parents=True)
            board = root / 'project' / meta['board']
            board_text = board.read_text()
            layers = sexpr(board_text).one('layers')
            # One origin for every file: the board's drill/place origin when it has one, else
            # KiCad's absolute origin (18 Nov: drill and positions could disagree with the
            # Gerbers; KiCad ignores its origin option when it plots with the board's settings).
            aux = re.search(r'\(aux_axis_origin\s+([-0-9.]+)\s+([-0-9.]+)\s*\)', board_text)
            place_origin = bool(aux and (float(aux.group(1)), float(aux.group(2))) != (0.0, 0.0))
            plotted = board
            aux_setting = re.compile(r'\(useauxorigin\s+(?:yes|no)\s*\)')
            if place_origin and not aux_setting.search(board_text):
                # 18 Nov review: the rewrite silently did nothing, while drill and positions moved.
                raise KiCadError('unsupported_project', {'detail': 'the board has a drill/place origin but no '
                                                         'plot setting for it; save the board once in KiCad'})
            if place_origin:
                plotted = staging.parent / ('plot-' + uuid.uuid4().hex) / board.name
                plotted.parent.mkdir()
                plotted.write_text(aux_setting.sub('(useauxorigin yes)', board_text))
            self._run('pcb', 'export', 'gerbers', '--board-plot-params',
                      '--output', str(staging) + '/', plotted)
            drill_options = ['--excellon-separate-th'] if arguments.get('separate_drills', False) else []
            drill_options += ['--drill-origin', 'plot' if place_origin else 'absolute']
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
                files[final] = staged_bytes(staging / name)
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
                files[name] = staged_bytes(final_dir / name)
            assembly = None
            if arguments.get('assembly'):
                # BOM and positions go beside the zip, not in it: JLC takes them separately.
                assembly, assembly_files = self._assembly_files(root, meta, board, place_origin)
                for name, data in assembly_files.items():
                    if unicodedata.normalize('NFC', name).casefold() in filename_keys or name in files:
                        raise KiCadError('rename_ambiguous')
                    (final_dir / name).write_bytes(data)
                    files[name] = data
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
            return {'files_written': written, 'comparison': comparison, 'staging_path': str(staging),
                    'origin': 'drill/place origin' if place_origin else 'absolute',
                    **({'assembly': assembly} if assembly is not None else {})}

    def _assembly_files(self, root, meta, board, place_origin):
        """JLC's BOM and positions, from KiCad's own exports of this copy (18 Nov), with each
        part's JLC correction read from its library footprint."""
        from alx.providers.kicad_assembly import BOM_FIELDS, BOM_GROUP, BOM_LABELS, correction, jlc_bom, jlc_positions
        from alx.providers.kicad_index import select
        schematic = board.with_suffix('.kicad_sch')
        if not schematic.is_file():
            raise KiCadError('unsupported_project', {'detail': 'no schematic for a BOM'})
        work = root / 'artifacts' / ('assembly-' + uuid.uuid4().hex)
        work.mkdir(parents=True)
        self._run('sch', 'export', 'bom', '--fields', BOM_FIELDS, '--labels', BOM_LABELS,
                  '--group-by', BOM_GROUP, '--ref-range-delimiter', '', '--exclude-dnp',
                  '--output', work / 'bom.csv', schematic)
        self._run('pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm', '--side', 'both',
                  '--exclude-dnp', *(['--use-drill-file-origin'] if place_origin else []),
                  '--output', work / 'pos.csv', board)
        bom, missing = jlc_bom((work / 'bom.csv').read_text(encoding='utf-8-sig'))
        ids = {o['ref']: o['footprint'] for o in select(self._board_index(board, root / 'artifacts'), 'part') if o['ref']}
        fixes, unread = {}, set()
        for footprint in sorted(set(ids.values())):
            try:
                text = self._library_parts([footprint.replace(':', '/', 1)], root / 'project', 'footprints')
            except KiCadError:
                unread.add(footprint)
                continue
            fix = correction(next(iter(text.values())))
            if fix:
                fixes[footprint] = fix
        positions, report = jlc_positions((work / 'pos.csv').read_text(encoding='utf-8-sig'),
                                          {ref: fixes[fp] for ref, fp in ids.items() if fp in fixes})
        bom_refs = {ref for line in csv.DictReader(io.StringIO(bom)) for ref in line['Designator'].split(',')}
        placed = {line['Designator'] for line in csv.DictReader(io.StringIO(positions))}
        return ({'bom_lines': bom.count('\n') - 1, 'missing_lcsc': missing, **report,
                 'corrections_used': {fp: {'rotation': fix[0], 'offset': list(fix[1])} for fp, fix in fixes.items()},
                 'footprints_not_in_library': sorted(unread)[:30],
                 'in_bom_not_placed': sorted(bom_refs - placed)[:30],
                 'placed_not_in_bom': sorted(placed - bom_refs)[:30]},
                {'bom.csv': bom.encode(), 'positions.csv': positions.encode()})

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
        # So is the edit history (revert_to), which keeps only the files edits changed.
        # His viewing windows' copies are not the work copy (17 Nov: KiCad's own .history,
        # a git store, in a view he saved refused every call on the copy).
        inventory(root, exclude={root / 'evidence', root / 'history', root / 'views'})
        evidence_bytes(root / 'evidence')
        evidence_bytes(root / 'history')
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

    def _erc(self, root, meta, signatures=None, against_original=False, *, project=None, findings=None, model=None):
        """KiCad's electrical rules check of the copy's schematic, grouped like the DRC.

        against_original: only findings his original did not already have are listed
        (totals stay whole); 16 Nov: every edit carried the whole ERC, some 30,000
        characters of his own findings, into the board agent's context.
        """
        baseline = None
        if against_original:
            try:
                baseline = json.loads((root / 'baseline.json').read_text())['erc']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        schematic = ((project or root / 'project') / meta['board']).with_suffix('.kicad_sch')
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
        context = self._erc_context(model)
        if (signatures is not None or baseline is not None) and model is None and any(
                finding.get('type') == 'power_pin_not_driven'
                for sheet in sheets for finding in sheet.get('violations') or ()):
            context = self._erc_context(self._netlist(schematic.parent, meta['board'], output.parent))
        known = None
        if baseline is not None:
            # Existing work copies stored [type, description, joined item descriptions].
            legacy = [mark for mark in baseline if isinstance(mark, list)]
            original_context = self._erc_context(None)
            if any(kind == 'power_pin_not_driven' for kind, _, _ in legacy):
                try:
                    original = self._original_view(root, meta)
                except KiCadError as error:
                    if str(error) != 'original_changed':
                        raise
                    # The old baseline still identifies findings by their recorded pins.
                    context = original_context
                else:
                    original_context = self._erc_context(self._netlist(original, meta['board'], output.parent))
            known = Counter(mark if isinstance(mark, str) else self._erc_key(
                {'type': mark[0], 'description': mark[1],
                 'items': [{'description': text} for text in mark[2].split('|')]}, original_context)
                for mark in baseline)
        totals = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}
        new = {'error': 0, 'warning': 0, 'ignore': 0}
        groups = {}
        for sheet in sheets:
            for finding in sheet.get('violations') or ():
                kind, severity = finding.get('type'), finding.get('severity')
                if not isinstance(kind, str) or severity not in ('error', 'warning', 'ignore'):
                    raise KiCadError('invalid_report')
                totals[severity] += 1
                totals['total'] += 1
                entry = {'type': kind, 'severity': severity, 'sheet': sheet.get('path', ''),
                         'description': str(finding.get('description', '')), 'items': finding.get('items') or []}
                if findings is not None:
                    findings.append(entry)
                if signatures is not None or known is not None:
                    mark = self._erc_key(entry, context)
                    if signatures is not None:
                        signatures.append(mark)
                    if known is not None:
                        if known[mark]:
                            known[mark] -= 1
                            continue
                        new[severity] += 1
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
        result = {'checked': True, 'reason': '', 'totals': totals, 'groups': ordered,
                  'top_level_sheets': top_level_sheets(schematic.parent)}
        if known is not None:
            result.update(only_new=True, new=dict(new, total=sum(new.values())))
        return result

    def _drc(self, root, meta, signatures=None):
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
        # The DRC also assigns temporary UUIDs to synthetic items such as "PCB".
        # Only UUIDs that occur in the drawing identify an object across runs.
        stored_ids = {node.children[1] for node in descendants(board)
                      if node.name in ('uuid', 'tstamp') and len(node.children) > 1}
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

        def group(entries, compared=True):
            groups = {}
            for finding in entries:
                kind, severity = finding['type'], finding['severity']
                if not isinstance(kind, str) or severity not in ('error', 'warning', 'ignore'):
                    raise KiCadError('invalid_report')
                totals[severity] += 1
                totals['total'] += 1
                if signatures is not None and compared:
                    # Every finding, before the limits, so a later check can say
                    # exactly what is new against his original (D-082).
                    first = (finding['items'] or [{}])[0].get('pos') or {}
                    signatures.append({'type': kind, 'severity': severity,
                                       'description': finding.get('description', ''),
                                       'ids': sorted(i['uuid'] for i in finding['items']
                                                     if i.get('uuid') in stored_ids),
                                       'items': sorted(str(item.get('description', ''))
                                                       for item in finding['items']),
                                       # Where it is, so it can be found and fixed (16 Nov); not
                                       # part of what makes a finding the same as his.
                                       'at': [round(float(first.get('x', 0)), 3), round(float(first.get('y', 0)), 3)]})
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
            # KiCad's own board-against-schematic check reads only the first
            # sheet; the comparison with his original uses the pin-by-pin one
            # across every sheet, so a DRC result depends on the board alone.
            result = {key: bounded_groups(group(report[key], key != 'schematic_parity')) for key in
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
        if bool(arguments.get('work_copy_id')) == bool(arguments.get('project_dir')):
            raise KiCadError('arguments_unusable', {'detail': 'name project_dir (his project, for a new copy) '
                                                    'or work_copy_id (one of yours, checked again)'})
        if arguments.get('work_copy_id'):
            return self._check_copy(arguments['work_copy_id'])
        source = self._project(arguments['project_dir'])
        if source.is_relative_to(self.work_root) or self.work_root.is_relative_to(source):
            raise KiCadError('path_denied')
        if not source.is_dir():
            raise KiCadError('unsupported_project', {'detail': 'not a folder: %s' % source})
        hashes = design_inventory(source)
        boards = [name for name in hashes if '/' not in name and name.endswith('.kicad_pcb')]
        if len(boards) != 1:
            raise KiCadError('unsupported_project', {'detail': self._where_boards_are(
                source, sorted(source.glob('*.kicad_pcb')))})
        work_id = uuid.uuid4().hex
        root = self.work_root / work_id
        root.mkdir(parents=True)
        shutil.copytree(source, root / 'project', ignore=_without_local_history(source))
        if design_inventory(source) != hashes or inventory(root / 'project') != hashes:
            raise KiCadError('original_changed')
        meta = {'source': str(source), 'hashes': hashes, 'board': boards[0]}
        (root / 'metadata.json').write_text(json.dumps(meta))
        signatures = []
        result = self._drc(root, meta, signatures)
        self._remember_drc(root, meta, self._board_totals(result), signatures)
        # His original as it was copied: each step's fingerprint, every DRC
        # finding and every schematic/board disagreement, so later work is
        # judged against it (D-082).
        steps = original_of(fingerprints(root / 'project', meta['board']))
        self._save_metadata(root, dict(meta, original_steps=steps))
        # The schematic is checked too, errors and warnings, aiming for a
        # clean result like the DRC (Friedl, 2026-11-13).
        baseline = {'drc': signatures, 'parity': self._parity(root, meta)}
        (root / 'baseline.json').write_text(json.dumps(baseline))
        erc_signatures = []
        result['erc'] = self._erc(root, meta, erc_signatures)
        (root / 'baseline.json').write_text(json.dumps(dict(baseline, erc=erc_signatures)))
        result['layout_rules'] = self._layout_rules(root / 'project' / meta['board'])
        result['compared_to_original'] = self._compared(root, meta, signatures)
        result['errors_to_show'] = errors_to_show(work_id, signatures)
        return dict(result, work_copy_id=work_id, work_copy_revision=self._revision(root))

    def _check_copy(self, work_copy_id):
        """One of her copies checked again as it is now, against his original (15 Nov:
        her helper tried the project check on a copy, four times refused)."""
        with self._locked(work_copy_id):
            root, meta = self._load(work_copy_id)
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            if meta.get('library'):
                raise KiCadError('arguments_unusable', {'detail': 'a library copy has no board to check'})
            signatures = []
            result = self._drc(root, meta, signatures)
            self._remember_drc(root, meta, self._board_totals(result), signatures)
            result['erc'] = self._erc(root, meta, against_original=True)
            result['layout_rules'] = self._layout_rules(root / 'project' / meta['board'])
            result['compared_to_original'] = self._compared(root, meta, signatures)
            result['errors_to_show'] = errors_to_show(work_copy_id, signatures)
            return dict(result, work_copy_id=work_copy_id, work_copy_revision=self._revision(root))

    def copy_library(self, arguments):
        """A work copy of a KiCad library folder (the FireFli library), for footprint and symbol work.

        Edited with the same tested helpers and written back with his approval,
        like a project. On 2026-11-13 hand-written scripts on his library crashed
        three times outside every guard the tool has.
        """
        named = str(arguments.get('library_dir') or '').strip()
        if named in ('', '${FIREFLI_KICAD_LIB}', '$FIREFLI_KICAD_LIB', 'FIREFLI_KICAD_LIB'):
            named = str(self._firefli_library())
        source = self._source(named)
        if source.is_relative_to(self.work_root) or self.work_root.is_relative_to(source) or not source.is_dir():
            raise KiCadError('path_denied' if source.is_dir() else 'unsupported_project')
        if source.suffix == '.pretty':
            # The library folder that holds it keeps the library's name (review of #240).
            raise KiCadError('unsupported_project', {'detail': 'name the folder that holds '
                                                     f'{source.name}, not the .pretty folder itself'})
        hashes = library_inventory(source)
        pretty = sorted({str(Path(name).parent) for name in hashes if name.endswith('.kicad_mod')})
        symbols = sorted(name for name in hashes if name.endswith('.kicad_sym'))
        if not pretty and not symbols:
            raise KiCadError('unsupported_project', {'detail': 'no footprint or symbol library in this folder'})
        work_id = uuid.uuid4().hex
        root = self.work_root / work_id
        root.mkdir(parents=True)
        try:
            shutil.copytree(source, root / 'project', ignore=_library_files(source))
            if library_inventory(source) != hashes or library_inventory(root / 'project') != hashes:
                raise KiCadError('original_changed')
        except BaseException:
            # A copy that did not complete is not kept (review of #240).
            shutil.rmtree(root, ignore_errors=True)
            raise
        meta = {'source': str(source), 'hashes': hashes, 'board': '', 'library': True}
        (root / 'metadata.json').write_text(json.dumps(meta))
        counts = Counter(str(Path(name).parent) for name in hashes if name.endswith('.kicad_mod'))
        return {'work_copy_id': work_id, 'work_copy_revision': self._revision(root),
                'footprint_libraries': [{'path': name, 'name': Path(name).stem, 'footprints': counts[name]}
                                        for name in pretty],
                'symbol_libraries': symbols}

    def _part_package(self, given):
        """A maker or SamacSys download in Parts-Inbox (a zip, folder or file), as its files."""
        from alx.providers.kicad_models import package_files
        inbox = getattr(self, 'parts_inbox', None)
        if inbox is None:
            raise KiCadError('unavailable', {'detail': 'no Parts-Inbox folder is configured'})
        name = Path(str(given).strip()).name
        source = Path(inbox) / name
        if not name or source.is_symlink() or not source.exists():
            present = sorted(p.name for p in Path(inbox).iterdir()) if Path(inbox).is_dir() else []
            raise KiCadError('arguments_unusable', {'detail': 'no %r in Parts-Inbox; it holds: %s'
                                                    % (name, ', '.join(present[:30]) or 'nothing')})
        return package_files(source)

    def _part_model(self, stem, name, spec, footprint_text, package, model_file, placement):
        """The part's 3D body: the download's STEP when its size is the datasheet's, else one
        generated from the datasheet's package size and height. Placed on the footprint."""
        from alx.providers.kicad_models import body_boxes, box_step, model_placement, step_extent, with_model
        path = '${FIREFLI_KICAD_LIB}/3dmodels/%s.3dshapes/%s.step' % (stem, name)
        steps = (package or {}).get('model', {})
        if model_file or steps:
            chosen = Path(str(model_file)).name if model_file else (next(iter(steps)) if len(steps) == 1 else None)
            if chosen not in steps:
                raise KiCadError('arguments_unusable', {'detail': 'model_file is one of: %s' % ', '.join(steps)})
            step = steps[chosen]
            extent = step_extent(step)
            if extent is None:
                raise KiCadError('arguments_unusable', {'detail': '%s is not a STEP model in mm' % chosen})
            if placement:
                offset = tuple(float(v) for v in placement.get('offset', (0, 0, 0)))
                rotate = tuple(float(v) for v in placement.get('rotate', (0, 0, 0)))
            else:
                found = next((model_placement(t.decode('utf-8', 'replace'))
                              for t in (package or {}).get('footprint', {}).values()), None)
                offset, rotate = found or ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
            size = _placed_extent(extent, offset, rotate)
            width, depth = (float(v) for v in spec['body'])
            fits = (abs((size[1][0] - size[0][0]) - width) <= max(0.3, width * 0.1)
                    and abs((size[1][1] - size[0][1]) - depth) <= max(0.3, depth * 0.1)
                    and -0.3 <= size[0][2] <= 0.3)
            if not fits:
                raise KiCadError('arguments_unusable', {'detail': (
                    '%s placed with offset %s and rotate %s spans x %.2f..%.2f, y %.2f..%.2f, z %.2f..%.2f mm; the '
                    'datasheet body is %.2f x %.2f mm on the board: give model_placement {offset, rotate} that puts '
                    'it there, or leave the STEP out and give footprint height for a generated body'
                    % ((chosen, list(offset), list(rotate)) + tuple(v for pair in zip(*size) for v in pair)
                       + (width, depth)))})
            return {'footprint': with_model(footprint_text, path, offset, rotate), 'step': step,
                    'source': chosen, 'size_mm': [round(size[1][i] - size[0][i], 3) for i in range(3)]}
        if 'height' not in spec:
            raise KiCadError('arguments_unusable', {'detail': 'no STEP in the download: give footprint height (the '
                                                    'package height from the datasheet) for a generated body'})
        try:
            pads = [(float(x), float(y), float(w), float(h)) for x, y, w, h in re.findall(
                r'\(pad "[^"]*" smd \w+\n\t\t\(at ([-\d.]+) ([-\d.]+)\)\n\t\t\(size ([\d.]+) ([\d.]+)\)', footprint_text)]
            boxes = body_boxes({'body': spec['body'], 'height': float(spec['height'])},
                               [(x - w / 2, y - h / 2, x + w / 2, y + h / 2) for x, y, w, h in pads])
        except (TypeError, ValueError) as error:
            raise KiCadError('arguments_unusable', {'detail': 'footprint height is a number in mm'}) from error
        return {'footprint': with_model(footprint_text, path), 'step': box_step(name, boxes), 'source': 'generated',
                'size_mm': [float(spec['body'][0]), float(spec['body'][1]), float(spec['height'])]}

    def _part_spice(self, root, project, name, spec, package):
        """The maker's SPICE model in the library, named by the symbol (KiCad's Sim fields), after
        ngspice has loaded it: {file, model, pins: {symbol pin: model node}}."""
        from alx.providers.kicad_models import DEVICE_NODES, KICAD_DEVICE, check_deck, spice_models
        if not isinstance(spec, dict) or not spec.get('model') or not isinstance(spec.get('pins'), dict):
            raise KiCadError('arguments_unusable', {'detail': 'spice is {file, model, pins: {symbol pin: model node}}'})
        files = (package or {}).get('spice', {})
        wanted = Path(str(spec.get('file') or '')).name
        if not wanted and len(files) == 1:
            wanted = next(iter(files))
        if wanted in files:
            source = files[wanted]
        else:
            inbox = getattr(self, 'parts_inbox', None)
            candidate = Path(inbox) / wanted if inbox and wanted else None
            if candidate is None or not candidate.is_file() or candidate.is_symlink():
                raise KiCadError('arguments_unusable', {'detail': 'spice file is one of: %s, or a file in Parts-Inbox'
                                                        % (', '.join(files) or 'none in the download')})
            source = candidate.read_bytes()
        text = source.decode('latin-1', 'replace')
        # A maker's file is model data. Any other directive (.control and its shell, .include, .lib)
        # is refused before ngspice reads it (review of #247), as clean_deck does for circuits.
        for line in text.splitlines():
            word = (line.split() or [''])[0].lower()
            if word.startswith('.') and word not in SPICE_MODEL_DIRECTIVES:
                raise KiCadError('arguments_unusable', {'detail': '%s holds %s, which is not model data; '
                                                        'only %s are taken' % (wanted, word,
                                                        ', '.join(sorted(SPICE_MODEL_DIRECTIVES)))})
        models = spice_models(text)
        model = models.get(str(spec['model']))
        if model is None:
            raise KiCadError('arguments_unusable', {'detail': '%s defines %s, not %s' % (
                wanted, ', '.join(models) or 'no model', spec['model'])})
        nodes = model['nodes'] if model['kind'] == 'subckt' else list(DEVICE_NODES.get(model['kind'], ()))
        pins = {str(k): str(v) for k, v in spec['pins'].items()}
        if sorted(pins.values()) != sorted(nodes):
            raise KiCadError('arguments_unusable', {'detail': 'pins must give each model node once: %s'
                                                    % ', '.join(nodes)})
        library_file = re.sub(r'[^A-Za-z0-9._+-]+', '_', name) + Path(wanted).suffix.lower()
        with tempfile.TemporaryDirectory(prefix='ngspice-', dir=root) as temporary:
            (Path(temporary) / library_file).write_bytes(source)
            (Path(temporary) / 'check.cir').write_text(check_deck(library_file, model, str(spec['model'])))
            # KiCad runs ngspice in PSpice/LTspice compatibility; makers' models are written for those
            # (a Diodes Inc BSIM3 LEVEL=7 model loads only so, 19 Nov 2026).
            (Path(temporary) / '.spiceinit').write_text('set ngbehavior=ltpsa\n')
            try:
                log = self._engine('ngspice', ['-b', 'check.cir'], temporary)
            except KiCadError as error:
                if error.args[0] != 'engine_failed':
                    raise
                log = str((error.recovery or {}).get('log') or 'ngspice stopped')
                raise KiCadError('arguments_unusable', {
                    'detail': 'ngspice could not load %s from %s' % (spec['model'], wanted),
                    'log': log[-1500:]}) from error
        if re.search(r"(?i)\berror\b|unknown subckt|could not find|can't find|no such|unrecognized", log):
            raise KiCadError('arguments_unusable', {'detail': 'ngspice could not load %s from %s' % (spec['model'], wanted),
                                                    'log': log[-1500:]})
        return {'file': library_file, 'source': source, 'model': str(spec['model']), 'kind': model['kind'],
                'fields': {'Sim.Library': '${FIREFLI_KICAD_LIB}/spice/' + library_file,
                           'Sim.Name': str(spec['model']), 'Sim.Device': KICAD_DEVICE[model['kind']],
                           'Sim.Pins': ' '.join('%s=%s' % (k, v) for k, v in sorted(pins.items()))}}

    def add_part(self, arguments):
        """A new part in a library work copy, built from its datasheet (19 Nov 2026): the symbol from
        the pin table, the footprint from the land pattern, named after its part number, with
        FireFli's fields and a catalogue row. Changes only the copy; the footprint is then measured
        against the datasheet drawing with check_kicad_layout before write_back."""
        from alx.providers.kicad_library_parts import part_name, with_catalogue_row, with_symbol
        from alx.providers.kicad_part_builder import PartSpecError, build_footprint, build_symbol
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if not meta.get('library'):
                raise KiCadError('arguments_unusable', {'detail': 'add_part needs a copy_library work copy'})
            project = root / 'project'
            library = str(arguments.get('library') or '').strip()
            mpn = str(arguments.get('mpn') or '').strip()
            missing = [key for key in ('library', 'mpn', 'manufacturer', 'description', 'datasheet', 'reference')
                       if not str(arguments.get(key) or '').strip()]
            if not arguments.get('pins'):
                missing.append('pins')
            shared = str(arguments.get('share_footprint') or '').strip()
            if not arguments.get('footprint') and not shared:
                missing.append('footprint (or share_footprint)')
            # A part is complete with its 3D body (Fusion) and its SPICE model or a stated reason
            # there is none from the maker (19 Nov 2026).
            if not arguments.get('spice') and not str(arguments.get('spice_none') or '').strip():
                missing.append('spice (the maker\'s model) or spice_none (why the maker publishes none)')
            if missing:
                raise KiCadError('arguments_unusable', {'detail': 'add_part needs ' + ', '.join(missing)})
            stem = 'FireFli_' + re.sub(r'^FireFli_', '', library)
            symbols_file = project / 'symbols' / (stem + '.kicad_sym')
            pretty = project / 'footprints' / (stem + '.pretty')
            catalogue = project / 'catalogue' / (stem + '.csv')
            if not (symbols_file.is_file() and pretty.is_dir() and catalogue.is_file()):
                raise KiCadError('arguments_unusable', {'detail': 'no library %s in this copy (symbols, footprints '
                                                        'and catalogue)' % stem})
            name = part_name(mpn)
            library_text = symbols_file.read_text()
            if ('(symbol %s' % json.dumps(name)) in library_text or (pretty / (name + '.kicad_mod')).exists() \
                    or any(row.get('MPN') == mpn for row in csv.DictReader(io.StringIO(catalogue.read_text()))):
                raise KiCadError('arguments_unusable', {'detail': '%s is already in %s: find_part gives its name'
                                                        % (mpn, stem)})
            if shared:
                # A reel variant of a package already in this library uses that footprint as it is.
                if not (pretty / (shared.split(':')[-1].split('/')[-1] + '.kicad_mod')).is_file():
                    raise KiCadError('arguments_unusable', {'detail': 'share_footprint names a footprint already '
                                                            'in %s' % stem})
                footprint_field = '%s:%s' % (stem, shared.split(':')[-1].split('/')[-1])
            else:
                footprint_field = '%s:%s' % (stem, name)
            fields = {'Reference': str(arguments['reference']).strip(), 'Value': mpn, 'Footprint': footprint_field,
                      'Datasheet': str(arguments['datasheet']).strip(),
                      'Description': str(arguments['description']).strip(),
                      'Manufacturer': str(arguments['manufacturer']).strip(), 'MPN': mpn,
                      'LCSC': str(arguments.get('lcsc') or '').strip()}
            package = self._part_package(arguments.get('files')) if arguments.get('files') else None
            spice = self._part_spice(root, project, name, plain(arguments.get('spice')), package) \
                if arguments.get('spice') else None
            if spice is not None:
                fields.update(spice['fields'])
            try:
                block = build_symbol(name, fields, plain(arguments['pins']))
                footprint_text, built = (None, None) if shared else build_footprint(
                    name, plain(arguments['footprint']), fields)
            except PartSpecError as error:
                raise KiCadError('arguments_unusable', {'detail': str(error)}) from error
            model = None
            if footprint_text is not None:
                model = self._part_model(stem, name, plain(arguments['footprint']), footprint_text, package,
                                         arguments.get('model_file'), plain(arguments.get('model_placement')))
                footprint_text = model.pop('footprint')
            new_library = with_symbol(library_text, block)
            try:
                sexpr(new_library)
                if footprint_text is not None:
                    sexpr(footprint_text)
            except Exception as error:  # noqa: BLE001 - a part KiCad could not read is never written
                raise KiCadError('arguments_unusable', {'detail': 'the new part does not read back: %s' % error})
            note = str(arguments.get('note') or '').strip()
            if spice is None:
                note = '; '.join(filter(None, (note, 'no SPICE model from the maker: '
                                               + str(arguments['spice_none']).strip())))
            row = {**{k: v for k, v in fields.items() if not k.startswith('Sim.')}, 'Note': note}
            new_catalogue = with_catalogue_row(catalogue.read_text(), row)
            # One change: kept whole, with an undo entry, or put back whole (review of #247).
            with self._change(root, meta):
                symbols_file.write_text(new_library)
                if footprint_text is not None:
                    (pretty / (name + '.kicad_mod')).write_text(footprint_text)
                    shapes = project / '3dmodels' / (stem + '.3dshapes')
                    shapes.mkdir(parents=True, exist_ok=True)
                    (shapes / (name + '.step')).write_bytes(model.pop('step'))
                if spice is not None:
                    (project / 'spice').mkdir(exist_ok=True)
                    (project / 'spice' / spice['file']).write_bytes(spice.pop('source'))
                catalogue.write_text(new_catalogue)
            return {'symbol': '%s:%s' % (stem, name), 'footprint': footprint_field, 'catalogue': row,
                    'built': dict(built or {'shared': footprint_field}, **({'model': model} if model else {}),
                                  **({'spice': {k: v for k, v in spice.items() if k != 'fields'}} if spice else {})),
                    'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': self._revision(root),
                    'next': ('measure the footprint against the datasheet land pattern with check_kicad_layout '
                             '(footprint %s/%s), correct it if it differs, then write_back with his approval'
                             % (stem, footprint_field.split(':', 1)[1]))}

    def _library_parts(self, names, project, kind):
        """Library footprints or symbols an edit names, read from KiCad's own library tables.

        Her script is confined to the copy and cannot read his library, so what
        it needs goes in with it: footprints by LIBRARY/NAME, symbols by
        LIBRARY:NAME, as KiCad names them.
        """
        if not names:
            return {}
        separator = '/' if kind == 'footprints' else ':'
        if (not isinstance(names, (list, tuple)) or len(names) > 20
                or not all(isinstance(n, str) and re.fullmatch(r'[^/\\:]+%s[^/\\:]+' % separator, n)
                           for n in names)):
            raise KiCadError('arguments_unusable', {'detail': '%s is a list of up to 20 LIBRARY%sNAME'
                                                    % (kind, separator)})
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        versions = sorted((p for p in (home / 'Library/Preferences/kicad').glob('*') if p.is_dir()),
                          key=lambda p: [int(x) for x in re.findall(r'\d+', p.name)] or [0])
        config = versions[-1] if versions else None
        variables = {'KIPRJMOD': str(project)}
        if config and (config / 'kicad_common.json').is_file():
            try:
                variables.update((json.loads((config / 'kicad_common.json').read_text())
                                  .get('environment') or {}).get('vars') or {})
            except (ValueError, AttributeError):
                pass
        tables = {}
        table_name = 'fp-lib-table' if kind == 'footprints' else 'sym-lib-table'
        for table in ([config / table_name] if config else []) + [project / table_name]:
            if table.is_file():
                for name, uri in re.findall(r'\(lib \(name "((?:[^"\\]|\\.)*)"\)\s*\(type "KiCad"\)\s*'
                                            r'\(uri "((?:[^"\\]|\\.)*)"\)', table.read_text(errors='replace')):
                    tables[name] = re.sub(r'\$\{(\w+)\}', lambda m: variables.get(m[1], m[0]), uri)
        # KiCad's own libraries (power, Device, ...) where his table does not list
        # them (15 Nov: five guesses at a GND symbol, every one refused).
        bundled = Path(variables.get('KICAD10_SYMBOL_DIR' if kind == 'symbols' else 'KICAD10_FOOTPRINT_DIR')
                       or (self.kicad_shared / ('symbols' if kind == 'symbols' else 'footprints')))
        for path in (bundled.glob('*.kicad_sym') if kind == 'symbols' else bundled.glob('*.pretty')):
            tables.setdefault(path.stem, str(path))
        found = {}
        for item in names:
            library, name = item.split(separator, 1)
            if kind == 'symbols' and (library not in tables or '${' in tables[library]):
                # A symbol the project already uses, from its own sheets' cache
                # (an Altium import's power symbols are in no library table).
                cached = self._cached_symbol(project, item)
                if cached is not None:
                    found[item] = cached
                    continue
            if library not in tables or '${' in tables[library]:
                raise KiCadError('arguments_unusable', {'detail': 'no %s library %s in KiCad or this project; '
                                                        'inspect_schematic lists what the project uses'
                                                        % (kind[:-1], library)})
            path = Path(tables[library]) / (name + '.kicad_mod') if kind == 'footprints' else Path(tables[library])
            limit = 2_000_000 if kind == 'footprints' else 32_000_000
            if (not path.is_file() or path.is_symlink() or denied(path.as_posix()) or path.stat().st_size > limit
                    or (kind == 'footprints' and path.resolve().parent != Path(tables[library]).resolve())):
                raise KiCadError('arguments_unusable', {'detail': 'no %s %s' % (kind[:-1], item)})
            text = path.read_text(errors='replace')
            if kind == 'symbols':
                tree = sexpr(text)
                block = [node for node in tree.named('symbol') if len(node.children) > 1
                         and node.children[1] == name]
                if not block:
                    raise KiCadError('arguments_unusable', {'detail': 'no symbol %s' % item})
                text = flat_symbol(text, tree, block[0], name)
            found[item] = text
        return found

    def _board_drc(self, root, meta):
        """DRC totals and findings of the copy's board, run again only when the board changed.

        15 Nov: every schematic edit ran the whole-board DRC twice, about 25
        seconds an edit, on a board it never touched.
        """
        try:
            last = json.loads((root / 'drc-last.json').read_text())
        except (OSError, ValueError):
            last = {}
        if last.get('board') == self._drc_key(root, meta):
            return last['totals'], last['signatures']
        signatures = []
        result = self._drc(root, meta, signatures)
        totals = self._board_totals(result)
        self._remember_drc(root, meta, totals, signatures)
        return totals, signatures

    @staticmethod
    def _board_totals(result):
        """DRC totals without KiCad's schematic-parity findings, which depend on the schematic,
        not the board, and so cannot be reused with a board-only key (review, 15 Nov)."""
        totals = dict(result['totals'])
        for group in result.get('schematic_parity', ()):
            count = group.get('count', 0)
            severity_counts = {}
            for finding in group.get('findings', ()):
                severity_counts[finding.get('severity')] = severity_counts.get(finding.get('severity'), 0) + 1
            omitted = group.get('omitted_findings', 0)
            # Findings beyond the kept examples share the group's first severity.
            first = next(iter(severity_counts), 'warning')
            severity_counts[first] = severity_counts.get(first, 0) + omitted
            for severity, n in severity_counts.items():
                if severity in totals:
                    totals[severity] = max(0, totals[severity] - n)
            totals['total'] = max(0, totals['total'] - count)
        return totals

    @staticmethod
    def _drc_key(root, meta):
        board = root / 'project' / meta['board']
        # The rules and the project's severities decide the result as much as the board. The
        # schematic is not in the key on purpose: KiCad's schematic-parity findings are left out of
        # everything reused (totals and signatures, 15 Nov), and the board-vs-schematic comparison
        # comes from _parity, fresh per schematic revision.
        files = (board, board.with_suffix('.kicad_dru'), board.with_suffix('.kicad_pro'))
        return hashlib.sha256(json.dumps(['drc-with-item-ids', *[sha(p) if p.is_file() else None
                                                               for p in files]]).encode()).hexdigest()

    def _remember_drc(self, root, meta, totals, signatures):
        (root / 'drc-last.json').write_text(json.dumps({'board': self._drc_key(root, meta), 'totals': totals,
                                                        'signatures': signatures}))

    @staticmethod
    def _sheet_has_symbol(project, lib_id):
        """Whether a sheet of the project already holds this library symbol."""
        needle = '(symbol %s' % json.dumps(lib_id)
        return any(needle in path.read_text(errors='replace') for path in project.glob('*.kicad_sch'))

    def _netlist(self, project, board_name, scratch):
        """KiCad's netlist of a project's schematic as parts and nets, or None without one.

        Exported once per schematic revision and kept beside the work (18 Nov review: five
        places each exported and read it their own way)."""
        from alx.providers.kicad_index import netlist_model
        schematic = (project / board_name).with_suffix('.kicad_sch')
        if not schematic.is_file():
            return None
        contents = {name: digest for name, digest in design_inventory(project).items()
                    if name.endswith(('.kicad_sch', '.kicad_pro', 'sym-lib-table'))}
        # Every sheet must be inside the project, so the key covers what the netlist depends on
        # (19 Nov recheck: a sheet outside it could change unseen).
        for name in contents:
            if name.endswith('.kicad_sch'):
                for file in re.findall(r'\(property\s+"Sheetfile"\s+"((?:[^"\\]|\\.)*)"',
                                       (project / name).read_text(errors='replace')):
                    # KiCad reads a sheet's file relative to the sheet that names it.
                    target = ((project / name).parent / file).resolve()
                    if (file.startswith(('/', '~')) or not target.is_relative_to(project.resolve())
                            or not target.is_file()):
                        raise KiCadError('unsupported_project', {'detail': 'sheet %s is outside the project or '
                                                                 'missing; the schematic cannot be compared' % file})
        # The key is everything the result depends on: which root, every sheet, the reader's own
        # version and KiCad's (18 Nov review: the root and versions were missing).
        kicad = Path(self.settings.kicad_cli)
        stamp = kicad.stat().st_mtime_ns if kicad.exists() else 0
        key = hashlib.sha256(json.dumps([NETLIST_VERSION, schematic.name, contents, str(kicad), stamp],
                                        sort_keys=True).encode()).hexdigest()[:32]
        scratch.mkdir(parents=True, exist_ok=True)
        kept = scratch / ('netlist-%s.json' % key)
        if kept.is_file():
            with suppress(ValueError, KeyError, TypeError, AttributeError):
                model = json.loads(kept.read_text())
                if (isinstance(model, dict) and model.get('version') == NETLIST_VERSION
                        and isinstance(model.get('nets'), list) and isinstance(model.get('components'), list)):
                    return model
        output = scratch / (uuid.uuid4().hex + '.net')
        try:
            self._run('sch', 'export', 'netlist', '--output', output, schematic)
            model = dict(netlist_model(output.read_text(errors='replace')), version=NETLIST_VERSION)
        finally:
            output.unlink(missing_ok=True)
        kept.write_text(json.dumps(model))
        self._keep_latest(scratch, 'netlist-')
        return model

    @staticmethod
    def _keep_latest(scratch, prefix, count=8):
        """Kept readings are bounded: only the newest few of each kind stay (18 Nov review: they
        grew inside the work copy, which _load holds to its size limit)."""
        kept = sorted(scratch.glob(prefix + '*.json'), key=lambda path: path.stat().st_mtime_ns)
        for old in kept[:-count]:
            old.unlink(missing_ok=True)

    def _board_index(self, board, scratch):
        """The one reading of a board (kicad_index), kept per board revision (18 Nov)."""
        from alx.providers.kicad_index import INDEX_VERSION, board_index
        text = board.read_text(errors='replace')
        key = hashlib.sha256(text.encode()).hexdigest()[:32]
        scratch.mkdir(parents=True, exist_ok=True)
        kept = scratch / ('index-%s.json' % key)
        if kept.is_file():
            with suppress(ValueError, KeyError, TypeError, AttributeError):
                index = json.loads(kept.read_text())
                # An index of an older shape is never used (18 Nov review).
                if (isinstance(index, dict) and index.get('version') == INDEX_VERSION
                        and isinstance(index.get('objects'), list) and isinstance(index.get('nets'), list)):
                    return index
        index = board_index(text)
        kept.write_text(json.dumps(index))
        self._keep_latest(scratch, 'index-')
        return index

    def _schematic_sync(self, project, meta, model):
        """Structured input for the script's sync_from_schematic helper."""
        parts, nets = {}, {}
        for comp in model['components']:
            parts[comp['ref']] = {'footprint': comp['footprint'], 'value': comp['value'],
                                  'fields': comp['fields'], 'path': comp['path']}
        for net in model['nets']:
            for node in net['nodes']:
                nets.setdefault(node['ref'], {})[node['pin']] = net['name']
        board_text = (project / meta['board']).read_text(errors='replace')
        on_board = {}
        for item in sexpr(board_text).named('footprint'):
            refs = [p.children[2] for p in item.named('property') if len(p.children) > 2 and p.children[1] == 'Reference']
            if refs:
                on_board[str(refs[0])] = str(item.children[1])
        footprints = sorted({info['footprint'].replace(':', '/', 1) for ref, info in parts.items()
                             if info['footprint'] and on_board.get(ref) != info['footprint']})
        return {'parts': parts, 'nets': nets, 'footprints': footprints,
                'not_in_schematic': sorted(set(on_board) - set(parts) - {''})}

    @staticmethod
    def _cached_symbol(project, lib_id):
        """A library symbol as one of the project's sheets keeps it, named back to its own name."""
        name = lib_id.split(':', 1)[1]
        for path in sorted(project.glob('*.kicad_sch')):
            text = path.read_text(errors='replace')
            if lib_id not in text:
                continue
            tree = sexpr(text)
            for cache in tree.named('lib_symbols'):
                for node in cache.named('symbol'):
                    if len(node.children) > 1 and node.children[1] == lib_id:
                        block = text[node.start:node.end]
                        return block.replace('(symbol %s' % json.dumps(lib_id), '(symbol %s' % json.dumps(name), 1)
        return None

    def _original_steps(self, root, meta):
        # Copies made before D-082 have none recorded; their original is
        # still his project, which write-back proves unchanged.
        if meta.get('original_steps'):
            return meta['original_steps']
        return original_of(fingerprints(Path(meta['source']), meta['board']))

    def _step_status(self, root, meta):
        current = fingerprints(root / 'project', meta['board'])
        return step_status(self._original_steps(root, meta), meta.get('approvals', {}), current)

    def _parity(self, root, meta):
        """Where the schematic and the board disagree, pin by pin, across every sheet; None without one."""
        from alx.providers.kicad_index import netlist_nets
        board = root / 'project' / meta['board']
        model = self._netlist(root / 'project', meta['board'], root / 'artifacts')
        if model is None:
            return None
        return parity_mismatches(netlist_nets(model), board.read_text())

    def _baseline(self, root):
        path = root / 'baseline.json'
        return json.loads(path.read_text()) if path.is_file() else None

    def _compared_parity(self, root, meta):
        """Schematic-vs-board differences new since his original. available False: there was no
        schematic to compare, which is not the same as no differences (18 Nov review: the flag
        was tested after None had become an empty list, so it always read True)."""
        now = self._parity(root, meta)
        base = (self._baseline(root) or {}).get('parity')
        found = now or []
        if base is None:
            # No comparison on his original: nothing can be called fixed, and everything found now
            # is new (18 Nov review: an unavailable baseline became an empty one).
            return {'new': sorted(shown_mismatch(line) for line in set(found)), 'fixed': 0,
                    'available': now is not None, 'baseline': False}
        # Compared in full; a baseline kept before 19 Nov holds lines already cut to twelve pins,
        # so those are matched as they were kept.
        old = set(base)
        new = sorted(line for line in set(found) if line not in old and shown_mismatch(line) not in old)
        fixed = [line for line in old if line not in set(found) and line not in {shown_mismatch(f) for f in found}]
        return {'new': [shown_mismatch(line) for line in new], 'fixed': len(fixed) if now is not None else 0,
                'available': now is not None, 'baseline': True}

    def _new_faults(self, root, signatures):
        """DRC errors and unconnected items found now that his original did not have."""
        base = self._baseline(root)
        if base is None:
            return []
        key = lambda f: json.dumps([f['type'], f['severity'], f['description'], f['items']])  # noqa: E731
        new = Counter(map(key, signatures)) - Counter(map(key, base['drc']))
        faults = [dict(zip(('type', 'severity', 'description', 'items'), json.loads(item)))
                  for item in new.elements()]
        return [f for f in faults if f['severity'] == 'error' or f['type'] == 'unconnected_items']

    def _compared(self, root, meta, signatures):
        """What the DRC finds now that it did not find on his original, and what it no longer finds."""
        base = self._baseline(root)
        if base is None:
            return {'available': False}
        key = lambda f: json.dumps([f['type'], f['severity'], f['description'], f['items']])  # noqa: E731
        before, after = Counter(map(key, base['drc'])), Counter(map(key, signatures))
        new, resolved = after - before, before - after
        parity = self._compared_parity(root, meta)
        places = {}
        for finding in signatures:
            places.setdefault(key(finding), []).append(finding.get('at'))
        found = []
        for item in new.elements():
            entry = dict(zip(('type', 'severity', 'description', 'items'), json.loads(item)))
            spots = places.get(item) or [None]
            entry['at'] = spots.pop() if len(spots) > 1 else spots[0]
            found.append(entry)
        found.sort(key=lambda entry: (entry['severity'] != 'error', entry['type']))
        by_type = Counter(entry['type'] for entry in found)
        return {'available': True, 'new': sum(new.values()), 'resolved': sum(resolved.values()),
                'new_by_type': dict(by_type), 'new_findings': found[:25],
                'schematic_vs_board_available': parity['available'],
                'schematic_vs_board_new': parity['new'][:20], 'schematic_vs_board_fixed': parity['fixed']}

    def _original_view(self, root, meta):
        """A copy of his project as the work copy was made from it, for looking at only."""
        view = root / 'original-view'
        if view.is_dir() and design_inventory(view) == meta['hashes']:
            return view
        shutil.rmtree(view, ignore_errors=True)
        source = Path(meta['source'])
        shutil.copytree(source, view, ignore=_without_local_history(source))
        if design_inventory(view) != meta['hashes']:
            shutil.rmtree(view, ignore_errors=True)
            raise KiCadError('original_changed')
        return view

    def inspect_schematic(self, arguments):
        """A copy's schematic as facts: every part with each pin's net, every net with what it joins.

        From KiCad's own netlist, across every sheet, with each symbol's sheet
        and place and each sheet's labels. One cheap read, where raw sheet text
        is large and says nothing directly about connections (15 Nov: the board
        agent spent 26 previews reading its way around a schematic).
        """
        area, file = arguments.get('area'), arguments.get('sheet')
        exact_ref, exact_net = arguments.get('ref'), arguments.get('net')
        if sum(value is not None for value in (exact_ref, exact_net, file)) > 1 or (
                area is not None and (not file or len(area) != 4 or not all(map(math.isfinite, area))
                                      or area[0] > area[2] or area[1] > area[3])) or (
                (exact_ref is not None or exact_net is not None) and any(
                    arguments.get(k) for k in ('refs', 'nets', 'contains', 'library_symbols'))):
            raise KiCadError('arguments_unusable', {'detail': 'use ref, net, or sheet with optional '
                                                    'ordered area [x0,y0,x1,y1] in mm'})
        named = arguments.get('project_dir') or arguments.get('work_copy_id')
        if arguments.get('project_dir') or not self._is_copy_id(named):
            # His project, read where it is: nothing is copied or written there (16 Nov:
            # five models asked for P0020's or P0009's schematic by project).
            board = self._board_for({'project_dir': named})
            project, meta, scratch = board.parent, {'board': board.name}, self.work_root / 'sections'
        else:
            root, meta = self._load(named)
            if meta.get('library'):
                raise KiCadError('arguments_unusable', {'detail': 'a library copy has no schematic'})
            project = self._original_view(root, meta) if arguments.get('original') else root / 'project'
            scratch = root / 'artifacts'
        schematic = (project / meta['board']).with_suffix('.kicad_sch')
        if not schematic.is_file():
            raise KiCadError('unsupported_project', {'detail': 'no schematic beside the board'})
        model = self._netlist(project, meta['board'], scratch)
        if exact_net is not None:
            chosen = [{'name': net['name'], 'nodes': [f"{pin['ref']}.{pin['pin']}" for pin in net['nodes']]}
                      for net in model['nets'] if net['name'] == exact_net]
            return {'sheets': [], 'parts': [], 'nets': chosen, 'library_symbols': [],
                    'truncated': {'parts': False, 'nets': False}}
        refs = {exact_ref} if exact_ref is not None else set(arguments.get('refs') or ())
        wanted_nets = set(arguments.get('nets') or ())
        needle = str(arguments.get('contains') or '').lower()
        limit = max(1, min(int(arguments.get('limit') or 200), 1000))
        pins, nets = {}, []
        for net in model['nets']:
            name = net['name']
            nodes = []
            for node in net['nodes']:
                ref, number = node['ref'], node['pin']
                pins.setdefault(ref, []).append({'number': number, 'name': node['function'], 'net': name})
                nodes.append('%s.%s' % (ref, number))
            nets.append({'name': name, 'nodes': nodes})
        places = {}
        sheets = []
        paths = sorted(project.glob('*.kicad_sch'))
        if exact_ref is not None:
            paths = [project / name for name in sorted(self._covered_sheets(project, meta['board'], model))]
        if file is not None:
            if file not in self._covered_sheets(project, meta['board'], model):
                raise KiCadError('unsupported_project', {'detail': f'{file} is outside the exported schematic hierarchy'})
            paths = [project / file]

        def in_area(at):
            return area is None or (area[0] <= at[0] <= area[2] and area[1] <= at[1] <= area[3])

        for path in paths:
            sheet = sexpr(path.read_text(errors='replace'))
            labels = []
            for node in sheet.children:
                if getattr(node, 'name', None) in ('label', 'global_label', 'hierarchical_label'):
                    positions = node.named('at')
                    if area is None or positions and in_area([float(v) for v in positions[0].children[1:3]]):
                        labels.append(str(node.children[1]))
            count = 0
            for symbol in sheet.named('symbol'):
                ref = next((str(p.children[2]) for p in symbol.named('property')
                            if len(p.children) > 2 and p.children[1] == 'Reference'), '')
                at = symbol.named('at')
                if ref and at:
                    position = [float(v) for v in at[0].children[1:3]]
                    if in_area(position):
                        count += 1
                        places[ref] = {'sheet': path.relative_to(project).as_posix(), 'at': position,
                                       'rotation': float(at[0].children[3]) if len(at[0].children) > 3 else 0.0}
            sheets.append({'file': path.relative_to(project).as_posix(), 'symbols': count,
                           'labels': sorted(set(labels))[:200]})
        parts = []
        for comp in model['components']:
            ref = comp['ref']
            if file is not None and ref not in places:
                continue
            item = {'ref': ref, 'value': comp['value'], 'footprint': comp['footprint'],
                    **places.get(ref, {}), 'pins': sorted(pins.get(ref, []), key=lambda p: (len(p['number']), p['number']))}
            haystack = ' '.join([ref, item['value'], item['footprint']]).lower()
            if (refs and ref not in refs) or (needle and needle not in haystack) or (
                    wanted_nets and not any(p['net'] in wanted_nets for p in item['pins'])):
                continue
            parts.append(item)
        if refs and parts:
            # Where each asked part's pins are on its sheet, which way they point and whether
            # wiring already ends there: what routing and clearing need (16 Nov).
            self._pin_geometry(project, meta, parts[:20])
        chosen = [net for net in nets if (not wanted_nets or net['name'] in wanted_nets)
                  and (not refs or any(node.split('.')[0] in refs for node in net['nodes']))]
        if exact_ref is not None:
            sheets = []
            chosen = []
        elif file is not None:
            # Parts already carry their pin-to-net connections. Use net for full membership.
            chosen = []
        # A library symbol's pins, before it is placed: what a swap's pin map needs.
        library = []
        names = [n for n in (arguments.get('library_symbols') or ())][:10]
        for name, block in (self._library_parts([n for n in names if not self._sheet_has_symbol(project, n)],
                                                project, 'symbols') if names else {}).items():
            library.append({'lib_id': name, 'pins': self._symbol_pins_from(block),
                            **self._symbol_notes_from(block)})
        for name in names:
            cached = self._cached_symbol(project, name) if self._sheet_has_symbol(project, name) else None
            if cached is not None:
                library.append({'lib_id': name, 'pins': self._symbol_pins_from(cached),
                                **self._symbol_notes_from(cached)})
        return {'sheets': sheets, 'parts': parts[:limit], 'nets': chosen[:limit], 'library_symbols': library,
                'truncated': {'parts': len(parts) > limit, 'nets': len(chosen) > limit}}

    @staticmethod
    def _pin_geometry(project, meta, parts):
        namespace = {'__name__': 'alx_sheet_reader'}
        exec(compile(SCHEMATIC_HELPERS, 'schematic_helpers', 'exec'), namespace)  # noqa: S102 - our own tested reader
        namespace['BOARD_PATH'] = str(project / meta['board'])
        opened = {}
        for part in parts:
            name = part.get('sheet')
            if not name:
                continue
            try:
                if name not in opened:
                    opened[name] = namespace['Sheet'](name)
                sheet = opened[name]
                placed = sheet.symbol(part['ref'])['pins']
                ends = {p for pts in sheet.wires() for p in (pts[0], pts[-1])} | {l['at'] for l in sheet.labels()}
            except (KeyError, ValueError, IndexError):
                continue
            for pin in part['pins']:
                point = placed.get(pin['number'])
                if point is None:
                    continue
                try:
                    direction = sheet.pin_direction(part['ref'], pin['number'])
                except (KeyError, ValueError, TypeError):
                    direction = None
                pin.update(at=list(point), points={(1, 0): 'right', (-1, 0): 'left', (0, 1): 'down',
                                                   (0, -1): 'up'}.get(direction, ''), wired=point in ends)

    @staticmethod
    def _symbol_notes_from(block):
        """A library part's own design notes (its Notes field), when it has them: what its
        datasheet asks of the board, kept on the part (16 Nov: the RFSW6024's NC pins)."""
        for node in sexpr(block).named('property'):
            if len(node.children) > 2 and str(node.children[1]) == 'Notes' and str(node.children[2]).strip():
                return {'notes': str(node.children[2]).strip()}
        return {}

    @staticmethod
    def _symbol_pins_from(block):
        """Each pin of a library symbol: number, name and electrical type."""
        pins = []
        for node in descendants(sexpr(block)):
            if node.name == 'pin' and len(node.children) > 1:
                number, name = node.named('number'), node.named('name')
                pins.append({'number': str(number[0].children[1]) if number else '',
                             'name': str(name[0].children[1]) if name else '',
                             'type': str(node.children[1])})
        return sorted(pins, key=lambda p: (len(p['number']), p['number']))

    def _export_pdf(self, what, board, made, *, options=()):
        """Draw the given revision with KiCad, for both published PDFs and edit feedback."""
        if what == 'schematic':
            schematic = board.with_suffix('.kicad_sch')
            if not schematic.is_file():
                raise KiCadError('unsupported_project', {'detail': 'no schematic beside the board'})
            self._run('sch', 'export', 'pdf', *options, '--output', made, schematic)
        else:
            inner = sorted(set(re.findall(r'"(In\d+\.Cu)"', board.read_text())),
                           key=lambda n: int(re.sub(r'\D', '', n)))
            copper = [] if what == 'placement' else ['F.Cu', *inner, 'B.Cu']
            # The fab layers carry every pad outline and number: where each pin points.
            layers = copper + ['F.Silkscreen', 'B.Silkscreen', 'F.Fab', 'B.Fab', 'Edge.Cuts']
            self._run('pcb', 'export', 'pdf', '--mode-single', '--sketch-pads-on-fab-layers', '--scale', '0',
                      *(('--black-and-white',) if what == 'placement' else ()),
                      '--layers', ','.join(layers), '--output', made, board)
        if not made.is_file():
            raise KiCadError('render_failed')

    def render(self, arguments):
        """KiCad's own drawing of a work copy, or of his original, as a PDF to look at."""
        what = arguments.get('what')
        if what not in ('schematic', 'board', 'placement'):
            raise KiCadError('arguments_unusable', {'detail': 'what is schematic, board or placement'})
        root, meta = self._load(arguments['work_copy_id'])
        if meta.get('library'):
            raise KiCadError('arguments_unusable', {'detail': 'a library copy has no board or schematic'})
        original = bool(arguments.get('original'))
        project = self._original_view(root, meta) if original else root / 'project'
        board = project / meta['board']
        name = '%s-%s.pdf' % (what, 'original' if original else self._revision(root)[:10])
        made = root / 'artifacts' / (uuid.uuid4().hex + '.pdf')
        made.parent.mkdir(exist_ok=True)
        self._export_pdf(what, board, made)
        folder = self.render_folder / arguments['work_copy_id']
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(made, folder / name)
        pages = max(1, made.read_bytes().count(b'/Type /Page') - made.read_bytes().count(b'/Type /Pages'))
        made.unlink(missing_ok=True)
        return {'path': str(folder / name), 'pages': pages, 'what': what, 'original': original}

    def open_in_kicad(self, arguments):
        """His design as it is beside her copy, in KiCad's own windows on his screen.

        Both are fresh copies made for looking; neither is his project or her
        work copy, so nothing he does in those windows changes either. Windows
        opened by an earlier call are closed first. A screenshot follows, for
        her to look at before she says anything.
        """
        what = arguments.get('what')
        if what not in ('schematic', 'board'):
            raise KiCadError('arguments_unusable', {'detail': 'what is schematic or board'})
        if self.open_viewer is None or self.capture_screen is None:
            raise KiCadError('unavailable')
        # Under the copy's lock, so the view shows exactly the revision it records (18 Nov review).
        with self._locked(arguments['work_copy_id']):
            return self._open_view(arguments, what)

    def _open_view(self, arguments, what):
        root, meta = self._load(arguments['work_copy_id'])
        if meta.get('library'):
            raise KiCadError('arguments_unusable', {'detail': 'a library copy has no board or schematic'})
        # An edit he saved in the last window and that is not in her copy yet is never lost: a new
        # window waits until it is taken (take_edits) or he says to drop it (19 Nov recheck: the
        # saved view was kept once, then deleted by the next window).
        seen = (meta.get('view') or {})
        last = root / 'views' / str(seen.get('stamp', '')) / 'amended'
        if seen and last.is_dir() and not arguments.get('discard_saved_edits') and self._saved_in_view(last, seen):
            raise KiCadError('arguments_unusable', {'detail': 'he saved an edit in the last KiCad window that is '
                                                    'not in your copy: take_edits first, or open with '
                                                    'discard_saved_edits true if he wants it dropped'})
        for viewer in self._viewers:
            with suppress(Exception):
                viewer.terminate()
        self._viewers = []
        for earlier in (root / 'views').glob('*') if (root / 'views').is_dir() else ():
            shutil.rmtree(earlier, ignore_errors=True)
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        views = root / 'views' / stamp
        folders = []
        # His original only when he wants to compare (Friedl, 15 Nov: "not sure this is needed").
        if arguments.get('beside_original'):
            shutil.copytree(self._original_view(root, meta), views / 'current')
            folders.append(views / 'current')
        shutil.copytree(root / 'project', views / 'amended')
        folders.append(views / 'amended')
        suffix = '.kicad_sch' if what == 'schematic' else '.kicad_pcb'
        files = [folder / Path(meta['board']).with_suffix(suffix).name for folder in folders]
        if not all(path.is_file() for path in files):
            raise KiCadError('unsupported_project', {'detail': 'no %s to open' % what})
        view = {'stamp': stamp, 'revision': self._revision(root, meta), 'what': what,
                'files': design_inventory(views / 'amended')}
        try:
            for index, path in enumerate(files):
                self._viewers.append(self.open_viewer(what, path, index))
        except OSError as error:
            raise KiCadError('unavailable', {'detail': 'KiCad did not open: %s' % error}) from error
        # The window is recorded as soon as it is open, so an edit he saves there can always be taken
        # in or protected (review of #247: a failed screenshot left an open, untracked window); it
        # counts as shown, for approval, only once the screen was taken.
        meta = dict(meta, view=dict(view, shown=False))
        self._save_metadata(root, meta)
        time.sleep(self.viewer_settle_seconds)
        folder = self.render_folder / arguments['work_copy_id']
        folder.mkdir(parents=True, exist_ok=True)
        shot = folder / ('screen-%s-%s.png' % (what, stamp))
        self.capture_screen(shot)
        if not shot.is_file():
            raise KiCadError('unavailable', {'detail': 'the screen could not be taken, so it is not known what he '
                                             'saw; check screen-recording permission'})
        meta = dict(meta, view=dict(view, shown=True))
        self._save_metadata(root, meta)
        return {'opened': [str(path) for path in files],
                'windows': (['current (his design)'] if len(files) == 2 else []) + ['amended (your copy)'],
                'screenshot': str(shot) if shot.is_file() else ''}

    @staticmethod
    def _saved_in_view(folder, view):
        """Whether he saved something in a KiCad window's copy since it opened; KiCad's lock files
        while the window is open are not the design (19 Nov recheck: a lock file alone counted)."""
        ignored = (lambda name: Path(name).name.startswith('~') or name.endswith('.lck'))
        now = {k: v for k, v in design_inventory(folder, exclude={folder / '.history'}).items() if not ignored(k)}
        then = {k: v for k, v in (view.get('files') or {}).items() if not ignored(k)}
        return now != then

    def take_edits(self, arguments):
        """Friedl's own edit, made and saved in the KiCad window open_in_kicad showed him, taken
        into her copy as one kept edit (17 Nov: he placed parts his way and wanted her to go on
        from there). Only while her copy is as it was when the window opened."""
        work_copy_id = arguments['work_copy_id']
        # Everything read under the lock (18 Nov review: the metadata was loaded before it).
        with self._locked(work_copy_id):
            root, meta = self._load(work_copy_id)
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            view = meta.get('view') or {}
            folder = root / 'views' / str(view.get('stamp', '')) / 'amended'
            if not view or not folder.is_dir():
                raise KiCadError('arguments_unusable', {'detail': 'no KiCad window of this copy was opened for him; '
                                                        'open_in_kicad first'})
            before = self._revision(root, meta)
            if before != view.get('revision'):
                raise KiCadError('arguments_unusable', {'detail': 'your copy changed after the window was opened '
                                                        'for him; open it again for his edit'})
            project = root / 'project'
            theirs, ours = design_inventory(folder, exclude={folder / '.history'}), design_inventory(project)
            # KiCad's lock files while his window is open are not the design.
            ignored = (lambda name: Path(name).name.startswith('~') or name.endswith('.lck'))
            changed = sorted(name for name in theirs if theirs[name] != ours.get(name) and not ignored(name))
            # A file he deleted there goes here too (18 Nov review: deletions were ignored).
            deleted = sorted(name for name in ours if name not in theirs and not ignored(name))
            if not changed and not deleted:
                return {'changed': [], 'moved': [], 'work_copy_revision': before,
                        'note': 'nothing saved in that window differs from your copy'}
            moved = []
            board = Path(meta.get('board') or '').name
            if board in changed:
                def places(text):
                    found = {}
                    for item in sexpr(text).named('footprint'):
                        refs = [p.children[2] for p in item.named('property')
                                if len(p.children) > 2 and p.children[1] == 'Reference']
                        at = item.named('at')
                        if refs and at:
                            found[str(refs[0])] = [str(v) for v in at[0].children[1:]]
                    return found
                old, new = places((project / board).read_text()), places((folder / board).read_text())
                moved = sorted(ref for ref in new if old.get(ref) != new[ref])
            steps_before = None if meta.get('library') else fingerprints(project, meta['board'])
            # One kept change: all of it or none of it (a failed copy no longer leaves half his edit).
            with self._change(root, meta) as copy_change:
                for name in changed:
                    (project / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(folder / name, project / name)
                for name in deleted:
                    (project / name).unlink()
                # What was taken is what he saved: KiCad may save again during the copy (19 Nov recheck).
                taken = design_inventory(project)
                if any(taken.get(name) != theirs[name] for name in changed) or \
                        design_inventory(folder, exclude={folder / '.history'}) != theirs:
                    raise KiCadError('verification_failed', {'detail': 'his window saved again while his edit was '
                                                             'being taken; take it again'})
                after = self._revision(root, meta)
                approvals = meta.get('approvals') or {}
                if steps_before is not None:
                    approvals = his_own_steps(approvals, steps_before, fingerprints(project, meta['board']))
                copy_change.meta = dict(meta, view=dict(view, revision=after, files=theirs), approvals=approvals)
            return {'changed': changed, 'deleted': deleted, 'moved': moved[:80], 'moved_total': len(moved),
                    'work_copy_revision': after}

    def approve_step(self, arguments):
        """Friedl approved one step of her copy as he saw it (D-082)."""
        step = arguments.get('step')
        if step not in STEPS:
            raise KiCadError('arguments_unusable', {'detail': 'step is schematic, placement or routing'})
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            if meta.get('library'):
                raise KiCadError('arguments_unusable', {'detail': 'a library copy has no steps'})
            revision = self._revision(root)
            if revision != arguments['work_copy_revision']:
                raise KiCadError('work_copy_changed')
            # He approves what he has seen: this exact copy opened for him in KiCad (17 Nov: asked
            # three times about one capacitor without being shown it once).
            view = meta.get('view') or {}
            shown = root / 'views' / str(view.get('stamp', '')) / 'amended'
            if view and shown.is_dir() and self._saved_in_view(shown, view):
                # He approves what is in front of him: an edit he saved there is taken first (19 Nov
                # recheck: the approval covered the board before his adjustment).
                raise KiCadError('arguments_unusable', {'detail': 'he saved an edit in the KiCad window: take_edits '
                                                        'first, then show him again and ask'})
            if view.get('revision') != revision or not view.get('shown'):
                raise KiCadError('arguments_unusable', {'detail': 'he has not been shown this version: open it '
                                                        'for him with open_in_kicad first, then ask'})
            shows = 'schematic' if step == 'schematic' else 'board'
            if view.get('what') != shows:
                raise KiCadError('arguments_unusable', {'detail': 'the %s step is approved on the %s: open the %s '
                                                        'for him with open_in_kicad first, then ask'
                                                        % (step, shows, shows)})
            current = fingerprints(root / 'project', meta['board'])
            original = self._original_steps(root, meta)
            approvals = dict(meta.get('approvals', {}))
            status = step_status(original, approvals, current)
            index = STEPS.index(step)
            if index and not ready(status, STEPS[index - 1]):
                raise KiCadError('step_not_approved', {
                    'needs': next(name for name in STEPS if status[name] not in ('unchanged', 'approved')),
                    'steps': status})
            if status[step] != 'unchanged':
                previous = approvals.get(step) or {}
                approvals[step] = {name: current[name] for name in STEPS[:index + 1]}
                if step == 'placement':
                    # The copper he saw with this placement, for a write-back before any routing.
                    approvals[step]['routing_seen'] = current['routing']
                # D-082b: approving a placement again, after it moved from what he approved, does not
                # stand in for the routing sign-off; routing stays due until he approves it seeing this
                # placement (19 Nov recheck: a second placement approval cleared the need).
                if step == 'placement' and previous and previous.get('placement') != current['placement']:
                    approvals['routing_due'] = True
                if step == 'routing':
                    approvals.pop('routing_due', None)
                self._save_metadata(root, dict(meta, approvals=approvals))
            return {'step': step, 'steps': step_status(original, approvals, current),
                    'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': revision}

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
        if not meta.get('library'):
            # Nothing reaches his project before he has approved every changed
            # step, and the board matches the schematic he approved (D-082).
            status = self._step_status(root, meta)
            # A placed board with no copper she added goes back for him to route (25 Nov: Friedl routes
            # for now; placement write-back was refused for want of a routing approval).
            # Copper removed after his placement approval still needs his routing approval (review of #247).
            copper = fingerprints(root / 'project', meta['board'])['routing']
            seen = ((meta.get('approvals') or {}).get('placement') or {}).get('routing_seen')
            placed_only = (all(status[step] in ('unchanged', 'approved') for step in ('schematic', 'placement'))
                           and not (meta.get('approvals') or {}).get('routing_due')
                           and copper in (self._original_steps(root, meta)['routing'], seen))
            if not ready(status, 'routing') and not placed_only:
                raise KiCadError('step_not_approved', {
                    'needs': next(step for step in STEPS if status[step] not in ('unchanged', 'approved')),
                    'steps': status})
            parity = self._compared_parity(root, meta)
            if not parity['available'] and any(name.endswith('.kicad_sch')
                                               for name in design_inventory(root / 'project')):
                # A project with a schematic is written back only once the board is compared with it
                # (18 Nov review: an unavailable comparison passed as "nothing new").
                raise KiCadError('schematic_board_differ', {'detail': 'the board could not be compared with '
                                                            'its schematic', 'new_mismatches': []})
            differing = parity['new']
            if differing:
                raise KiCadError('schematic_board_differ', {'new_mismatches': differing[:20]})
            # Nor with a DRC error or an unconnected item his original did not
            # have (his done-lines; review, 15 Nov: approvals alone were checked).
            _totals, signatures = self._board_drc(root, meta)
            new = self._new_faults(root, signatures)
            if not (meta.get('approvals') or {}).get('routing'):
                # Before his routing sign-off the board goes back placed, for him to route (D-082):
                # the ratsnest is the work he takes over, not a fault (25 Nov).
                new = [f for f in new if f['type'] != 'unconnected_items']
            if new:
                raise KiCadError('board_not_clean', {'new_faults': new[:20], 'count': len(new)})

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
        if not meta.get('library'):
            stray = icloud_copies(copied, root / 'project')
            copied = {name: digest for name, digest in copied.items()
                      if name in meta['hashes'] or not (name.endswith('.kicad_sch' + GRID_SUFFIX)
                                                        or name in stray)}
        changed = sorted((name for name, value in copied.items() if meta['hashes'].get(name) != value),
                         key=lambda name: (name not in meta['hashes'], name))
        # Any KiCad design file may be written back: a part change edits the
        # schematic as well as the board (2026-11-13, C62 and C66). Anything
        # else, or a file removed, is still refused, and named.
        own = ((lambda name: _library_file(Path(name).parent, Path(name).name)) if meta.get('library')
               else is_design_file)
        other = [name for name in changed if not own(name)]
        missing = sorted(set(meta['hashes']) - set(copied))
        if other or missing:
            raise KiCadError('unsupported_project', {
                'not_design_files': other[:20], 'removed_files': missing[:20]})
        if not meta.get('library'):
            # Every part on his boards comes from the FireFli library, built from its datasheet with
            # its part number (19 Nov 2026: the LED circuit was drawn with KiCad's generic parts).
            # Parts already in his project stay as they are; a new one must be a library part.
            added = sorted(set(_non_library_parts(root / 'project')) - set(_non_library_parts(source)))
            if added:
                raise KiCadError('library_parts_required', {
                    'detail': 'these parts are not FireFli library parts: find_part, or add_part from the '
                              'datasheet in a copy_library work copy and write that back first, then swap them',
                    'parts': ['%s (%s)' % item for item in added[:30]]})
        backup = root / 'backups' / uuid.uuid4().hex
        backup.mkdir(parents=True)

        def verified_copy(origin, target, digest):
            shutil.copy2(origin, target)
            if sha(target) != digest or target.read_bytes() != origin.read_bytes():
                raise KiCadError('verification_failed')

        def beside(name, kind):
            target = source / name
            if not target.parent.is_dir():
                # A new part can bring the library's first folder of its kind (3dmodels/<lib>.3dshapes,
                # spice); a design write-back never adds folders (review of #247, 26 Nov).
                if not meta.get('library') or not target.parent.resolve().is_relative_to(source.resolve()):
                    raise KiCadError('unsupported_project')
                target.parent.mkdir(parents=True, exist_ok=True)
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
