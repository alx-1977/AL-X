"""Bounded KiCad mechanics (D-058); never interprets user language."""
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
import uuid

from alx.providers.own_source import denied
from alx.providers.coding_process import KICAD_CLI_UNAVAILABLE_ERRORS, KICAD_CLI_TIMEOUT

logger = logging.getLogger(__name__)



# What one check result carries per DRC check type (D-057a).
FINDINGS_PER_CHECK_TYPE = 8
NOISE_EXAMPLES = 2
ITEMS_PER_FINDING = 4

MAX_PROJECT_ENTRIES = 20000
MAX_PROJECT_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024


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


def inventory(root, *, exclude=()):
    result = {}
    total = 0
    entries = 0
    for path in root.rglob('*'):
        # Only exact transaction-owned paths, never a filename pattern.
        if path in exclude:
            continue
        entries += 1
        if entries > MAX_PROJECT_ENTRIES:
            raise KiCadError('project_too_large')
        bounded(path, (root,), protected=False)
        if denied(path.relative_to(root).as_posix()):
            raise KiCadError('path_denied')
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise KiCadError('path_denied')
        if path.is_file():
            total += path.stat().st_size
            if path.stat().st_size > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES:
                raise KiCadError('project_too_large')
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_library(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise KiCadError('project_too_large')
    return path.read_text()


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


def silk_text(node):
    """Shared text identification, including layers with knockout flags."""
    if node.name not in ('fp_text', 'gr_text', 'property'):
        return False
    layers = node.named('layer')
    return bool(layers and len(layers[0].children) > 1
                and layers[0].children[1] in ('F.SilkS', 'B.SilkS'))


BOARD_INVARIANT_VERSION = 2


def board_canonical(board_source, *, version=BOARD_INVARIANT_VERSION):
    """Everything except permitted dimensions and derived caches is invariant."""
    if type(version) is not int or version not in (1, BOARD_INVARIANT_VERSION):
        raise KiCadError('unsupported_project')
    def canonical(node, context='', zone=False):
        if not isinstance(node, Node):
            return node
        zone = zone or node.name == 'zone'
        silk = silk_text(node)
        children = []
        for child in node.children:
            if isinstance(child, Node) and (
                    (context == 'font' and child.name in ('size', 'thickness'))
                    or (version != 1 and silk and child.name == 'render_cache')
                    or (zone and child.name in ('filled_polygon', 'fill_segments'))):
                continue
            child_context = ''
            if isinstance(child, Node):
                if silk and child.name == 'effects':
                    child_context = 'effects'
                elif context == 'effects' and child.name == 'font':
                    child_context = 'font'
            value = canonical(child, child_context, zone)
            # Dimension-only wrappers may be inserted when effects/font are absent.
            if child_context and value == [child.name]:
                continue
            children.append(value)
        return children
    return canonical(sexpr(board_source))


def invariant_digest(tree):
    return hashlib.sha256(json.dumps(tree).encode()).hexdigest()


def board_invariant(board_source):
    return invariant_digest(board_canonical(board_source))


# Fixed diagnostic vocabulary: arbitrary node names are board content too.
DIAGNOSTIC_NODE_NAMES = frozenset({
    'kicad_pcb', 'footprint', 'property', 'fp_text', 'gr_text', 'dimension',
    'effects', 'font', 'size', 'thickness', 'face', 'justify', 'layer', 'hide',
    'at', 'uuid', 'tstamp', 'pad', 'segment', 'zone', 'polygon', 'pts', 'xy',
    'start', 'end', 'width', 'net', 'layers', 'setup', 'general', 'version',
    'generator', 'render_cache',
})


def first_board_difference(expected, current):
    """Return canonical child indices and allowlisted names, never atom values."""
    def name(tree):
        value = tree[0] if isinstance(tree, list) and tree else None
        return value if isinstance(value, str) and value in DIAGNOSTIC_NODE_NAMES else 'unknown'

    def compare(left, right, path):
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                if index >= len(left) or index >= len(right):
                    child = left[index] if index < len(left) else right[index]
                    return path + [index] + ([name(child)] if isinstance(child, list) else [])
                if left[index] != right[index]:
                    child_path = path + [index]
                    if isinstance(left[index], list) or isinstance(right[index], list):
                        child_path.append(name(left[index] if isinstance(left[index], list) else right[index]))
                    return compare(left[index], right[index], child_path)
        return path
    return None if expected == current else compare(expected, current, [name(expected)])


def verify_board_invariant(board_source, meta, expected=None):
    # Absence means the historical format, never permission to rebaseline.
    version = meta.get('board_invariant_version', 1)
    current = board_canonical(board_source, version=version)
    if invariant_digest(current) != meta['board_invariant']:
        expected = meta.get('board_canonical', expected)
        path = first_board_difference(expected, current) if expected is not None else ['kicad_pcb']
        logger.warning('KiCad board_structure_changed path=%s', json.dumps(path))
        raise KiCadError('board_structure_changed', {'first_difference_path': path})
    if version == 1:
        current = board_canonical(board_source)
        meta.update(board_invariant_version=BOARD_INVARIANT_VERSION,
                    board_invariant=invariant_digest(current), board_canonical=current)
        return True
    return False


SAME_PART_CONDITION = "A.Type == 'Pad' && B.Type == 'Pad' && A.Parent == B.Parent"


def condition_tokens(expression):
    """Ignore expression whitespace, preserving literal contents and token boundaries."""
    return tuple(re.findall(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[A-Za-z_][A-Za-z_0-9]*|[0-9.]+|==|!=|&&|\|\||\S", expression))


class KiCadProvider:
    def __init__(self, settings, execute_cli):
        self.settings, self.execute_cli = settings, execute_cli
        self.design_roots = tuple(p.expanduser().resolve() for p in settings.design_roots)
        self.work_root = settings.work_root.expanduser().resolve()

    def _run(self, *args):
        subcommand = 'pcb export svg' if args[:3] == ('pcb', 'export', 'svg') else 'pcb drc'
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
        inventory(root)
        meta = json.loads((root / 'metadata.json').read_text())
        if meta.get('board_invariant_version') != BOARD_INVARIANT_VERSION:
            self._verify_board(root, meta, (root / 'project' / meta['board']).read_text())
        return root, meta

    def _revision(self, root, meta=None):
        contents = inventory(root / 'project')
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

    def _drc(self, root, meta):
        output = root / 'artifacts' / (uuid.uuid4().hex + '.json')
        output.parent.mkdir(exist_ok=True)
        before_source = (root / 'project' / meta['board']).read_text()
        expected = board_canonical(before_source)
        self._verify_board(root, meta, before_source)
        self._run('pcb', 'drc', '--format', 'json', '--severity-all', '--refill-zones',
                  '--output', output, root / 'project' / meta['board'])
        try:
            report = json.loads(output.read_text())
        except (ValueError, OSError) as error:
            raise KiCadError('invalid_report') from error
        if not isinstance(report, dict) or not all(isinstance(report.get(k), list) for k in
                   ('violations', 'unconnected_items', 'schematic_parity')):
            raise KiCadError('invalid_report')
        board_text = (root / 'project' / meta['board']).read_text()
        verify_board_invariant(board_text, meta, expected)
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

    def _verify_board(self, root, meta, board_source):
        if verify_board_invariant(board_source, meta):
            # Persist the verified upgrade before CLI refill or authorised edits.
            self._save_metadata(root, meta)

    def check(self, arguments):
        source = bounded(arguments['project_dir'], self.design_roots)
        if source.is_relative_to(self.work_root) or self.work_root.is_relative_to(source):
            raise KiCadError('path_denied')
        if not source.is_dir():
            raise KiCadError('unsupported_project')
        hashes = inventory(source)
        boards = [name for name in hashes if '/' not in name and name.endswith('.kicad_pcb')]
        if len(boards) != 1:
            raise KiCadError('unsupported_project')
        work_id = uuid.uuid4().hex
        root = self.work_root / work_id
        root.mkdir(parents=True)
        shutil.copytree(source, root / 'project')
        if inventory(source) != hashes or inventory(root / 'project') != hashes:
            raise KiCadError('original_changed')
        canonical = board_canonical((root / 'project' / boards[0]).read_text())
        meta = {'source': str(source), 'hashes': hashes, 'board': boards[0],
                'board_invariant_version': BOARD_INVARIANT_VERSION,
                'board_invariant': invariant_digest(canonical), 'board_canonical': canonical}
        (root / 'metadata.json').write_text(json.dumps(meta))
        result = self._drc(root, meta)
        return dict(result, work_copy_id=work_id, work_copy_revision=self._revision(root))

    def _render(self, root, meta):
        target = root / 'artifacts' / uuid.uuid4().hex
        target.mkdir()
        self._run('pcb', 'export', 'svg', '--layers', 'F.Cu,B.Cu,F.SilkS,B.SilkS,Edge.Cuts',
                  '--mode-multi', '--output', str(target) + '/',
                  root / 'project' / meta['board'])
        paths = [str(p) for p in target.glob('*.svg') if p.is_file()]
        if not paths:
            raise KiCadError('render_failed')
        return paths

    def fix(self, arguments):
        silk_minima = {'silk_text_height': 'silk_text_min_height_mm',
                       'silk_text_thickness': 'silk_text_min_thickness_mm'}
        if any(key in arguments for key in silk_minima.values()) and not arguments.get('silkscreen_min_text', False):
            raise KiCadError('arguments_unusable')
        clearance = arguments.get('same_footprint_pad_clearance_mm')
        for key in ('same_footprint_pad_clearance_mm', *silk_minima.values()):
            if key not in arguments:
                continue
            value = arguments[key]
            try:
                valid = (not isinstance(value, bool)
                         and isinstance(value, (int, float))
                         and math.isfinite(value) and value > 0)
            except OverflowError:
                valid = False
            if not valid:
                raise KiCadError('arguments_unusable')
        root, meta = self._load(arguments['work_copy_id'])
        board = root / 'project' / meta['board']
        paths = (board, board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru'))
        snapshot = {path: path.read_bytes() if path.exists() else None for path in paths}
        # Persist uncertainty BEFORE even the before DRC, which refills zones.
        pending = dict(meta, taint=uuid.uuid4().hex)
        self._save_metadata(root, pending)
        stage = 'before_drc'
        try:
            before = self._drc(root, meta)
            stage = 'render'
            before_images = self._render(root, meta)
            stage = 'template'
            board = root / 'project' / meta['board']
            pro = board.with_suffix('.kicad_pro')
            if not pro.is_file():
                raise KiCadError('unsupported_project')
            settings = json.loads(pro.read_text())
            original_settings = json.dumps(settings, sort_keys=True)
            changes = []
            template = arguments.get('apply_template')
            if template:
                template_root = self.settings.template_root.expanduser().resolve()
                if Path(template).name != template or template in ('.', '..'):
                    raise KiCadError('path_denied')
                folder = bounded(template_root / template, (template_root,))
                files = inventory(folder)
                projects = [name for name in files if name.endswith('.kicad_pro')]
                if len(projects) != 1:
                    raise KiCadError('unsupported_project')
                template_project = folder / projects[0]
                template_settings = json.loads(template_project.read_text())['board']['design_settings']
                if 'rules' not in template_settings:
                    raise KiCadError('unsupported_project')
                current = settings['board']['design_settings']
                for key in ('rules', 'rule_severities'):
                    if key in template_settings:
                        if current.get(key) != template_settings[key]:
                            current[key] = template_settings[key]
                            changes.append({'kind': 'template_' + key, 'value': template})
                dru = template_project.with_suffix('.kicad_dru')
                if dru.is_file():
                    target = board.with_suffix('.kicad_dru')
                    if not target.exists() or target.read_bytes() != dru.read_bytes():
                        target.write_bytes(dru.read_bytes())
                        changes.append({'kind': 'template_custom_rules', 'value': template})
            stage = 'clearance'
            if clearance is not None:
                target = board.with_suffix('.kicad_dru')
                original_rules = target.read_text() if target.exists() else '(version 1)\n'
                # Synthetic root permits span edits across top-level rules and comments.
                parsed = sexpr('(' + original_rules + '\n)')
                managed_name = 'ALX same-footprint pad clearance'
                managed = [node for node in parsed.named('rule') if node.children[1] == managed_name]
                existing = [node for node in parsed.named('rule')
                            if node.children[1] != managed_name
                            and any(len(condition.children) == 2 and
                                    condition_tokens(condition.children[1]) == condition_tokens(SAME_PART_CONDITION)
                                    for condition in node.named('condition'))]
                if existing:
                    for node in existing:
                        changes.append({'kind': 'same_footprint_pad_clearance',
                                        'value': node.children[1], 'status': 'already_present',
                                        'rule_name': node.children[1]})
                else:
                    rule = (f'(rule "{managed_name}"\n'
                            f'  (condition {json.dumps(SAME_PART_CONDITION)})\n'
                            f'  (constraint clearance (min {clearance!r}mm)))\n')
                    updated_rules = original_rules
                    for node in reversed(managed):
                        updated_rules = updated_rules[:node.start - 1] + updated_rules[node.end - 1:]
                    updated_rules = updated_rules.rstrip() + '\n' + rule
                    if updated_rules != original_rules:
                        target.write_text(updated_rules)
                        changes.append({'kind': 'same_footprint_pad_clearance',
                                        'value': str(clearance), 'clearance_mm': clearance})
            stage = 'noise'
            if arguments.get('ignore_noise_checks', False):
                severities = settings['board']['design_settings'].setdefault('rule_severities', {})
                for kind in self.settings.noise_checks:
                    if severities.get(kind) != 'ignore':
                        severities[kind] = 'ignore'
                        changes.append({'kind': 'rule_severity', 'value': kind})
            stage = 'silkscreen'
            if arguments.get('silkscreen_min_text', False):
                rules = settings.get('board', {}).get('design_settings', {}).get('rules', {})
                missing = [key for key, argument in silk_minima.items()
                           if argument not in arguments and key not in rules]
                if missing:
                    raise KiCadError('unsupported_project', {'missing_settings': missing})
                height, thickness = (float(arguments[argument] if argument in arguments else rules[key])
                                     for key, argument in silk_minima.items())
                if not all(math.isfinite(v) and v > 0 for v in (height, thickness)):
                    raise KiCadError('unsupported_project')
                board_source = board.read_text()
                board_tree = sexpr(board_source)
                edits = []
                for node in descendants(board_tree):
                    if not silk_text(node):
                        continue
                    effects = node.one('effects') if node.named('effects') else None
                    # Skip the owner name and text fields: the parser represents
                    # quoted "hide" text and bare visibility tokens identically.
                    visibility_start = 2 if node.name == 'gr_text' else 3
                    if any('hide' in owner.children[start:] or any(
                            hidden.children[1:] == ['yes'] for hidden in owner.named('hide'))
                           for owner, start in ((node, visibility_start), (effects, 1))
                           if owner is not None):
                        continue
                    dimensions = f'(size {height:g} {height:g}) (thickness {thickness:g})'
                    if effects is None:
                        edits.append((node.end - 1, node.end - 1,
                                      f' (effects (font {dimensions}))'))
                        continue
                    if not effects.named('font'):
                        edits.append((effects.end - 1, effects.end - 1, f' (font {dimensions})'))
                        continue
                    font = effects.one('font')
                    if not font.named('size'):
                        edits.append((font.end - 1, font.end - 1, f' (size {height:g} {height:g})'))
                    else:
                        size = font.one('size')
                        x, y = map(float, size.children[1:])
                        if not all(math.isfinite(v) and v > 0 for v in (x, y)):
                            raise KiCadError('unsupported_project')
                        if x < height or y < height:
                            edits.append((size.start, size.end, f'(size {max(x, height):g} {max(y, height):g})'))
                    if not font.named('thickness'):
                        edits.append((font.end - 1, font.end - 1, f' (thickness {thickness:g})'))
                        continue
                    width = font.one('thickness')
                    if float(width.children[1]) < thickness:
                        edits.append((width.start, width.end, f'(thickness {thickness:g})'))
                # Glyph polygons are derived from the old dimensions. Force KiCad
                # to regenerate them, just as it refills the zone cache.
                for node in descendants(board_tree):
                    if silk_text(node) and any(node.start < start < node.end
                                               for start, _, _ in edits):
                        edits.extend((cache.start, cache.end, '')
                                     for cache in node.named('render_cache'))
                for start, end, value in sorted(edits, reverse=True):
                    board_source = board_source[:start] + value + board_source[end:]
                if edits:
                    board.write_text(board_source)
                    changes.append({'kind': 'silkscreen_dimensions', 'value': str(len(edits)),
                                    'silk_text_min_height_mm': height,
                                    'silk_text_min_thickness_mm': thickness})
            if json.dumps(settings, sort_keys=True) != original_settings:
                pro.write_text(json.dumps(settings, indent=2) + '\n')
            stage = 'after_drc'
            after = self._drc(root, meta)
            stage = 'render'
            after_images = self._render(root, meta)
            result = {'work_copy_id': arguments['work_copy_id'],
                    'before': dict(before['totals'], custom_rule_citations=before['custom_rule_citations'],
                                   work_copy_tainted=before['work_copy_tainted'], baseline_eligible=before['baseline_eligible']),
                    'after': dict(after['totals'], custom_rule_citations=after['custom_rule_citations'],
                                  work_copy_tainted=after['work_copy_tainted'], baseline_eligible=after['baseline_eligible']),
                    'work_copy_tainted': bool(meta.get('taint')), 'baseline_eligible': not bool(meta.get('taint')),
                    'changes': changes,
                    'before_images': before_images, 'after_images': after_images}
            stage = 'revision'
            result['work_copy_revision'] = self._revision(root, meta)
            stage = 'metadata'
            self._save_metadata(root, meta)
        except BaseException as error:
            if not isinstance(error, (KiCadError, KeyboardInterrupt, SystemExit)):
                logger.warning('KiCad apply failed during %s: %s', stage, type(error).__name__)
            restored = True
            for path, original in snapshot.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
                except OSError:
                    restored = False
            # Verify the entire snapshot after all restorations, including absence.
            for path, original in snapshot.items():
                try:
                    if (path.exists() if original is None else path.read_bytes() != original):
                        restored = False
                except OSError:
                    restored = False
            if getattr(error, 'details', {}).get('reason_code') == 'session_unstoppable':
                restored = False
            if restored:
                try:
                    self._save_metadata(root, meta)
                except OSError:
                    restored = False
            recovery = {'work_copy_id': arguments['work_copy_id'],
                        'rollback_status': 'restored' if restored else 'failed',
                        'work_copy_tainted': bool(meta.get('taint')) or not restored,
                        'baseline_eligible': not bool(meta.get('taint')) and restored}
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, KiCadError):
                error.recovery = dict(error.recovery or {}, **recovery)
                raise
            recovery['error'] = {'code': 'verification_failed', 'type': type(error).__name__,
                                 'stage': stage}
            raise KiCadError('verification_failed', recovery) from error
        return result

    def find(self, arguments):
        library_id = arguments.get('library_id')
        if bool(library_id) == bool(arguments.get('designator')):
            raise KiCadError('arguments_unusable')
        if not library_id:
            root, meta = self._load(arguments.get('work_copy_id'))
            board = sexpr((root / 'project' / meta['board']).read_text())
            matches = [fp.children[1] for fp in board.named('footprint')
                       if arguments['designator'] in references(fp)]
            if len(matches) != 1:
                raise KiCadError('footprint_ambiguous')
            library_id = matches[0]
        parts = library_id.split(':')
        if len(parts) not in (1, 2) or any(not p or '/' in p or '\\' in p or p in ('.', '..') for p in parts):
            raise KiCadError('path_denied')
        library_root = self.settings.library_root.expanduser().resolve()
        candidates = []
        folders = ([library_root / (parts[0] + '.pretty')] if len(parts) == 2
                   else list(library_root.glob('*.pretty')))
        for folder in folders:
            path = bounded(folder / (parts[-1] + '.kicad_mod'), (library_root,))
            if path.is_file():
                parsed = sexpr(read_library(path))
                candidates.append({'library_id': folder.stem + ':' + path.stem,
                                   'path': str(path), 'pad_count': len(parsed.named('pad'))})
        return {'candidates': candidates}

    def write_back(self, arguments):
        recovery = {'backed_up': [], 'written': [], 'rollback_status': 'not_needed',
                    'restorations': [], 'remaining_changed': [], 'cleanup_errors': [],
                    'write_digests': {}, 'conflicts': [],
                    'error': {'code': '', 'type': ''}}
        try:
            return self._write_back(arguments, recovery)
        except Exception as error:
            code = str(error) if isinstance(error, KiCadError) else 'unavailable'
            if isinstance(error, KiCadError) and error.recovery:
                recovery.update(error.recovery)
            recovery['error'] = {'code': code, 'type': type(error).__name__}
            raise KiCadError(code, recovery) from error

    def _write_back(self, arguments, recovery):
        root, meta = self._load(arguments['work_copy_id'])
        source = bounded(arguments['project_dir'], self.design_roots)
        if str(source) != meta['source']:
            raise KiCadError('path_denied')
        if self._revision(root) != arguments['work_copy_revision']:
            raise KiCadError('work_copy_changed')
        self._verify_board(root, meta, (root / 'project' / meta['board']).read_text())

        temporary = set()
        expected = dict(meta['hashes'])

        def validate():
            bounded(source, self.design_roots)
            if list(source.rglob('~*.lck')):
                raise KiCadError('board_open')
            current = inventory(source, exclude=temporary)
            if current != expected:
                raise KiCadError('original_changed')
        validate()
        copied = inventory(root / 'project')
        changed = sorted((name for name, value in copied.items() if meta['hashes'].get(name) != value),
                         key=lambda name: (name not in meta['hashes'], name))
        allowed = {str(Path(meta['board']).with_suffix(s)) for s in ('.kicad_pcb', '.kicad_pro', '.kicad_dru')}
        if set(changed) - allowed or set(meta['hashes']) - set(copied):
            raise KiCadError('unsupported_project')
        backup = root / 'backups' / uuid.uuid4().hex
        backup.mkdir(parents=True)

        def verified_copy(origin, target, digest):
            shutil.copy2(origin, target)
            if sha(target) != digest or target.read_bytes() != origin.read_bytes():
                raise KiCadError('verification_failed')

        def beside(name, kind):
            fd, value = tempfile.mkstemp(prefix='.' + name + '.' + kind + '-', dir=source)
            os.close(fd)
            path = Path(value)
            temporary.add(path)
            return path

        staged = {}
        try:
            for name in changed:
                if name in meta['hashes']:
                    target = backup / name
                    recovery['backed_up'].append(str(target))
                    verified_copy(source / name, target, meta['hashes'][name])
            for name in changed:
                staged[name] = beside(name, 'stage')
                verified_copy(root / 'project' / name, staged[name], copied[name])
            validate()
            if self._revision(root) != arguments['work_copy_revision']:
                raise KiCadError('work_copy_changed')
            for name in changed:
                target = bounded(source / name, self.design_roots)
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
                name = target.name
                digest = None
                try:
                    bounded(target, self.design_roots)
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
                    bounded(target, self.design_roots)
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
