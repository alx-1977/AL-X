"""Bounded KiCad mechanics (D-061, amending D-058); never interprets user language."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zipfile
import difflib
import unicodedata
from collections import Counter
from contextlib import contextmanager
import fcntl

from alx.providers.own_source import denied
from alx.providers.reference_file import reference_path, read_reference, archive_entries, read_bytes, credential_path
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

    def _source(self, path):
        raw = Path(path).expanduser().absolute()
        if any(raw.resolve().is_relative_to(r) for r in self.design_roots):
            reference_path(raw, roots=self.design_roots)
            return bounded(raw, self.design_roots)
        return reference_path(raw)

    def read_reference(self, arguments):
        return read_reference(arguments)

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
            board = project / meta['board']
            original = inventory(project)
            snapshot = root / ('snapshot-' + uuid.uuid4().hex)
            shutil.copytree(project, snapshot)
            if inventory(snapshot) != original:
                raise KiCadError('verification_failed')
            self._save_metadata(root, dict(meta, taint=uuid.uuid4().hex))
            script_path = root / ('edit-' + uuid.uuid4().hex + '.py')
            stage = 'before_drc'
            keep_snapshot = False
            try:
                before = self._drc(root, meta)['totals']
                facts_before = board_facts(board.read_text())
                script_path.write_text(script)
                stage = 'script'
                executed = self.execute_python(str(self.settings.kicad_python), script_path, board)
                stdout, stderr = (getattr(executed, key, '') or '' for key in ('stdout', 'stderr'))
                if executed.returncode:
                    raise KiCadError('script_failed', {'stdout': stdout[:16000], 'stderr': stderr[:16000]})
                inventory(project)
                stage = 'after_drc'
                after = self._drc(root, meta)['totals']
                current = inventory(project)
                facts_after = board_facts(board.read_text())
                changed = sorted(name for name in original.keys() | current.keys()
                                 if original.get(name) != current.get(name))
                report = {'files_changed': changed[:200], 'files_truncated': len(changed) > 200,
                          'board': structural_diff(facts_before, facts_after)}
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
                    # The report is cut short: keep the before-edit copy and the
                    # complete diff, so every omitted change stays reviewable.
                    full = root / 'artifacts' / ('edit-diff-' + uuid.uuid4().hex + '.diff')
                    full.parent.mkdir(exist_ok=True)
                    with full.open('w') as stream:
                        for name in changed:
                            left = (snapshot / name).read_text(errors='replace') if name in original else ''
                            right = (project / name).read_text(errors='replace') if name in current else ''
                            stream.writelines(difflib.unified_diff(
                                left.splitlines(True), right.splitlines(True),
                                fromfile=name + ':before', tofile=name + ':after'))
                    keep_snapshot = True
                    report.update(full_diff_path=str(full), before_copy_path=str(snapshot))
                # The pending taint is cleared only after all verification succeeds.
                revision = self._revision(root, meta)
                self._save_metadata(root, meta)
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
                recovery = dict(getattr(error, 'recovery', None) or {},
                    work_copy_id=arguments['work_copy_id'], rollback_status='restored' if restored else 'failed',
                    work_copy_tainted=not restored, baseline_eligible=restored, stage=stage)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                code = str(error) if isinstance(error, KiCadError) else (
                    'cli_timeout' if isinstance(error, KICAD_CLI_TIMEOUT) else 'verification_failed')
                raise KiCadError(code, recovery) from error
            finally:
                script_path.unlink(missing_ok=True)
                # Retain snapshot evidence when recovery cannot be verified.
                if not keep_snapshot and not json.loads((root / 'metadata.json').read_text()).get('taint'):
                    shutil.rmtree(snapshot)

    def export(self, arguments):
        with self._locked(arguments['work_copy_id']):
            root, meta = self._load(arguments['work_copy_id'])
            if meta.get('taint'):
                raise KiCadError('work_copy_tainted')
            relative = Path(arguments['output_dir'])
            if relative.is_absolute() or not relative.parts or any(p in ('.', '..') for p in relative.parts):
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
        subcommand = ' '.join(args[:3]) if args[:2] == ('pcb', 'export') else 'pcb drc'
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
        meta = {'source': str(source), 'hashes': hashes, 'board': boards[0]}
        (root / 'metadata.json').write_text(json.dumps(meta))
        result = self._drc(root, meta)
        return dict(result, work_copy_id=work_id, work_copy_revision=self._revision(root))

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
        with self._locked(arguments['work_copy_id']):
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
            current = inventory(source, exclude=temporary)
            if current != expected:
                raise KiCadError('original_changed')
        validate()
        copied = inventory(root / 'project')
        changed = sorted((name for name, value in copied.items() if meta['hashes'].get(name) != value),
                         key=lambda name: (name not in meta['hashes'], name))
        if (any(Path(name).suffix not in ('.kicad_pcb', '.kicad_pro', '.kicad_dru') for name in changed)
                or set(meta['hashes']) - set(copied)):
            raise KiCadError('unsupported_project')
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
