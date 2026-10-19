"""Bounded KiCad mechanics (D-058); never interprets user language."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from alx.providers.own_source import denied

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


def board_invariant(board_source):
    """Everything except permitted text dimensions and zone-fill cache is invariant."""
    def canonical(node, silk=False, zone=False):
        if not isinstance(node, Node):
            return node
        if node.name in ('fp_text', 'gr_text', 'property'):
            layers = node.named('layer')
            silk = bool(layers and layers[0].children[1] in ('F.SilkS', 'B.SilkS'))
        zone = zone or node.name == 'zone'
        return [canonical(child, silk, zone) for child in node.children
                if not (isinstance(child, Node) and (
                    (silk and node.name == 'font' and child.name in ('size', 'thickness'))
                    or (zone and child.name in ('filled_polygon', 'fill_segments'))))]
    return hashlib.sha256(json.dumps(canonical(sexpr(board_source))).encode()).hexdigest()


class KiCadProvider:
    def __init__(self, settings, runner=subprocess.run):
        self.settings, self.runner = settings, runner
        self.design_roots = tuple(p.expanduser().resolve() for p in settings.design_roots)
        self.work_root = settings.work_root.expanduser().resolve()

    def _run(self, *args):
        result = self.runner([str(self.settings.kicad_cli), *map(str, args)],
                             capture_output=True, timeout=120, check=False)
        if result.returncode != 0:
            raise KiCadError('cli_failed')

    def _load(self, work_copy_id):
        if not isinstance(work_copy_id, str) or len(work_copy_id) != 32 or any(
                c not in '0123456789abcdef' for c in work_copy_id):
            raise KiCadError('path_denied')
        root = bounded(self.work_root / work_copy_id, (self.work_root,), protected=False)
        inventory(root)
        meta = json.loads((root / 'metadata.json').read_text())
        return root, meta

    def _revision(self, root):
        return hashlib.sha256(json.dumps(inventory(root / 'project'), sort_keys=True).encode()).hexdigest()

    def _drc(self, root, meta):
        output = root / 'artifacts' / (uuid.uuid4().hex + '.json')
        output.parent.mkdir(exist_ok=True)
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
        if board_invariant(board_text) != meta['board_invariant']:
            raise KiCadError('board_structure_changed')
        board = sexpr(board_text)
        identities = {}
        for footprint in board.named('footprint'):
            refs = references(footprint)
            ref = refs[0] if refs else ''
            for node in [footprint, *footprint.named('pad')]:
                for uid in node.named('uuid') + node.named('tstamp'):
                    identities[uid.children[1]] = (ref, node.children[1] if node.name == 'pad' else '')
        totals = {'error': 0, 'warning': 0, 'ignore': 0, 'total': 0}

        def group(entries):
            groups = {}
            for finding in entries:
                kind, severity = finding['type'], finding['severity']
                if not isinstance(kind, str) or severity not in ('error', 'warning', 'ignore'):
                    raise KiCadError('invalid_report')
                totals[severity] += 1
                totals['total'] += 1
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
            result = {key: group(report[key]) for key in
                      ('violations', 'unconnected_items', 'schematic_parity')}
        except (KeyError, TypeError, AttributeError) as error:
            raise KiCadError('invalid_report') from error
        result['totals'] = totals
        return result

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
        meta = {'source': str(source), 'hashes': hashes, 'board': boards[0],
                'board_invariant': board_invariant((root / 'project' / boards[0]).read_text())}
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
        root, meta = self._load(arguments['work_copy_id'])
        before = self._drc(root, meta)['totals']
        before_images = self._render(root, meta)
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
        if arguments.get('ignore_noise_checks', False):
            severities = settings['board']['design_settings'].setdefault('rule_severities', {})
            for kind in self.settings.noise_checks:
                if severities.get(kind) != 'ignore':
                    severities[kind] = 'ignore'
                    changes.append({'kind': 'rule_severity', 'value': kind})
        if arguments.get('silkscreen_min_text', False):
            rules = settings['board']['design_settings']['rules']
            height = float(rules['silk_text_height'])
            thickness = float(rules['silk_text_thickness'])
            if not all(math.isfinite(v) and v > 0 for v in (height, thickness)):
                raise KiCadError('unsupported_project')
            board_source = board.read_text()
            edits = []
            for node in descendants(sexpr(board_source)):
                if node.name not in ('fp_text', 'gr_text', 'property'):
                    continue
                layers = node.named('layer')
                if not layers or layers[0].children[1] not in ('F.SilkS', 'B.SilkS'):
                    continue
                font = node.one('effects').one('font')
                size = font.one('size')
                x, y = map(float, size.children[1:])
                if not all(math.isfinite(v) and v > 0 for v in (x, y)):
                    raise KiCadError('unsupported_project')
                if x < height or y < height:
                    edits.append((size.start, size.end, f'(size {max(x, height):g} {max(y, height):g})'))
                width = font.one('thickness')
                if float(width.children[1]) < thickness:
                    edits.append((width.start, width.end, f'(thickness {thickness:g})'))
            for start, end, value in sorted(edits, reverse=True):
                board_source = board_source[:start] + value + board_source[end:]
            if edits:
                board.write_text(board_source)
                changes.append({'kind': 'silkscreen_dimensions', 'value': str(len(edits))})
        if json.dumps(settings, sort_keys=True) != original_settings:
            pro.write_text(json.dumps(settings, indent=2) + '\n')
        after = self._drc(root, meta)['totals']
        after_images = self._render(root, meta)
        return {'work_copy_id': arguments['work_copy_id'], 'work_copy_revision': self._revision(root),
                'before': before, 'after': after, 'changes': changes,
                'before_images': before_images, 'after_images': after_images}

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
            recovery['error'] = {'code': code, 'type': type(error).__name__}
            raise KiCadError(code, recovery) from error

    def _write_back(self, arguments, recovery):
        root, meta = self._load(arguments['work_copy_id'])
        source = bounded(arguments['project_dir'], self.design_roots)
        if str(source) != meta['source']:
            raise KiCadError('path_denied')
        if self._revision(root) != arguments['work_copy_revision']:
            raise KiCadError('work_copy_changed')
        if board_invariant((root / 'project' / meta['board']).read_text()) != meta['board_invariant']:
            raise KiCadError('board_structure_changed')

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
