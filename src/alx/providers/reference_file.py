"""Bounded reference facts from user-owned files; no interpretation."""
from __future__ import annotations

import io
import os
import stat
import sys
import fcntl
from pathlib import Path, PurePosixPath
import struct
import zipfile

from alx.providers.own_source import denied

MAX_BYTES = 32 * 1024 * 1024
MAX_TEXT = 24000
MAX_ENTRIES = 200
MAX_PAGES = 20


class ReferenceError(Exception):
    pass


def credential_path(path):
    parts = str(path).casefold().replace('\\', '/').split('/')
    return denied('/'.join(parts)) or any(
        p in ('.ssh', '.gnupg', '.aws', '.azure', 'gcloud', 'keychains', '.pypirc', '.git')
        or 'keychain' in p for p in parts)


def reference_path(path, *, roots=None, protected=True):
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ReferenceError('path_denied')
    resolved = raw.resolve()
    roots = (Path.home().resolve(),) if roots is None else roots
    inside = [r for r in roots if resolved.is_relative_to(r)]
    # An unprotected root is trusted as a whole; names below it are still checked.
    checked = (raw, resolved) if protected else tuple(resolved.relative_to(r) for r in inside)
    if not inside or any(credential_path(p) for p in checked):
        raise ReferenceError('path_denied')
    return resolved


def archive_entries(bundle):
    entries = bundle.infolist()
    if len(entries) > 20000 or sum(e.file_size for e in entries) > 256 * 1024 * 1024:
        raise ReferenceError('project_too_large')
    names = set()
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                or credential_path(entry.filename) or entry.filename in names
                or entry.file_size > MAX_BYTES or entry.flag_bits & 1
                or (entry.external_attr >> 16) & 0o170000 == 0o120000):
            raise ReferenceError('path_denied')
        names.add(entry.filename)
    return entries


def read_bytes(path):
    path = Path(path).absolute()
    if credential_path(path):
        raise ReferenceError('path_denied')
    file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(file_fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReferenceError('unsupported_file')
        # Verify the opened object, rather than re-resolving a name that may
        # have been swapped. This also excludes ancestor-symlink substitution.
        if sys.platform == 'darwin':
            actual = os.fsdecode(fcntl.fcntl(file_fd, 50, b'\0' * 1024).split(b'\0', 1)[0])  # F_GETPATH
        elif sys.platform.startswith('linux'):
            actual = os.readlink(f'/proc/self/fd/{file_fd}')
        else:
            raise ReferenceError('unavailable')
        if actual != str(path) or credential_path(actual):
            raise ReferenceError('path_denied')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ReferenceError('project_too_large')
    return data


def ole_stream_names(data):
    """Read the CFB directory, bounded to the already bounded input.

    Altium binary streams differ across versions. Directory names are facts
    even when the proprietary board records cannot be decoded reliably.
    """
    if data[:8] != bytes.fromhex('d0cf11e0a1b11ae1') or len(data) < 512:
        raise ReferenceError('unsupported_file')
    sector_size = 1 << struct.unpack_from('<H', data, 30)[0]
    if sector_size not in (512, 4096):
        raise ReferenceError('unsupported_file')
    def sector(index):
        start = (index + 1) * sector_size
        if index >= len(data) // sector_size or start + sector_size > len(data):
            raise ReferenceError('unsupported_file')
        return data[start:start + sector_size]
    difat = list(struct.unpack_from('<109I', data, 76))
    next_difat, count = struct.unpack_from('<II', data, 68)
    seen = set()
    for _ in range(min(count, len(data) // sector_size)):
        if next_difat in seen:
            raise ReferenceError('unsupported_file')
        seen.add(next_difat)
        values = struct.unpack('<' + 'I' * (sector_size // 4), sector(next_difat))
        difat.extend(values[:-1])
        next_difat = values[-1]
    fat = []
    fat_indices = [i for i in difat if i < 0xfffffffa]
    if len(fat_indices) > len(data) // sector_size or len(set(fat_indices)) != len(fat_indices):
        raise ReferenceError('unsupported_file')
    for index in difat:
        if index < 0xfffffffa:
            fat.extend(struct.unpack('<' + 'I' * (sector_size // 4), sector(index)))
    index = struct.unpack_from('<I', data, 48)[0]
    seen, directory = set(), []
    while index < 0xfffffffa:
        if index in seen or index >= len(fat):
            raise ReferenceError('unsupported_file')
        seen.add(index)
        chunk = sector(index)
        if len(directory) + sector_size // 128 > 20000:
            raise ReferenceError('project_too_large')
        for offset in range(0, sector_size, 128):
            size = struct.unpack_from('<H', chunk, offset + 64)[0]
            name = chunk[offset:offset + max(0, min(size - 2, 62))].decode('utf-16-le', errors='replace')
            directory.append({'name': name, 'type': chunk[offset + 66],
                'left': struct.unpack_from('<I', chunk, offset + 68)[0],
                'right': struct.unpack_from('<I', chunk, offset + 72)[0],
                'child': struct.unpack_from('<I', chunk, offset + 76)[0]})
        index = fat[index]
    if not directory or directory[0]['type'] != 5:
        raise ReferenceError('unsupported_file')
    pending, visited, names = [(directory[0]['child'], '')], set(), []
    while pending:
        index, parent = pending.pop()
        if index == 0xffffffff:
            continue
        if index >= len(directory) or index in visited:
            raise ReferenceError('unsupported_file')
        visited.add(index)
        entry = directory[index]
        name = parent + entry['name']
        if len(name) > 4096:
            raise ReferenceError('project_too_large')
        if entry['type'] == 2:
            if len(names) <= MAX_ENTRIES:
                names.append(name[:512])
        elif entry['type'] == 1:
            pending.append((entry['child'], name + '/'))
        pending.extend((entry[side], parent) for side in ('left', 'right'))
    return {'kind': 'altium_ole', 'streams': names[:MAX_ENTRIES],
            'truncated': len(names) > MAX_ENTRIES, 'board_records_decoded': False}


def content_facts(name, data, start=0):
    suffix = Path(name).suffix.casefold()
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        width, height = struct.unpack('>II', data[16:24])
        return dict(kind='image', media_type='image/png', width=width, height=height)
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
        width, height = struct.unpack('<HH', data[6:10])
        return dict(kind='image', media_type='image/gif', width=width, height=height)
    if data.startswith(b'BM') and len(data) >= 26:
        width, height = struct.unpack_from('<ii', data, 18)
        return dict(kind='image', media_type='image/bmp', width=abs(width), height=abs(height))
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP' and len(data) >= 30:
        if data[12:16] == b'VP8X':
            width = 1 + int.from_bytes(data[24:27], 'little')
            height = 1 + int.from_bytes(data[27:30], 'little')
        elif data[12:16] == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
            width, height = (v & 0x3fff for v in struct.unpack_from('<HH', data, 26))
        elif data[12:16] == b'VP8L' and data[20] == 0x2f:
            bits = int.from_bytes(data[21:25], 'little')
            width, height = 1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)
        else:
            raise ReferenceError('unsupported_file')
        return dict(kind='image', media_type='image/webp', width=width, height=height)
    if data.startswith(b'\xff\xd8'):
        offset = 2
        while offset + 4 < len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            offset += 2
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            size = int.from_bytes(data[offset:offset + 2], 'big')
            if size < 2:
                break
            if marker in (0xc0, 0xc1, 0xc2) and offset + 7 <= len(data):
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                return dict(kind='image', media_type='image/jpeg', width=width, height=height)
            offset += size
        raise ReferenceError('unsupported_file')
    if suffix == '.pdf' or data.startswith(b'%PDF-'):
        from pypdf import PdfReader
        from alx.providers.pdf_limits import enforce_pdf_decode_limits
        enforce_pdf_decode_limits()
        reader = PdfReader(io.BytesIO(data))
        pages, budget = [], MAX_TEXT
        for index in range(min(len(reader.pages), MAX_PAGES)):
            page = reader.pages[index]
            # Bound both stored and decompressed content before text extraction.
            stored = page.get('/Contents', [])
            stored = stored if isinstance(stored, list) else [stored]
            if sum(len(getattr(s.get_object(), '_data', b'')) for s in stored) > 4 * 1024 * 1024:
                raise ReferenceError('project_too_large')
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > 4 * 1024 * 1024:
                raise ReferenceError('project_too_large')
            text = page.extract_text() or ''
            pages.append({'page': index + 1, 'text': text[:min(4000, budget)],
                          'truncated': len(text) > min(4000, budget)})
            budget -= len(pages[-1]['text'])
            if budget <= 0:
                break
        return dict(kind='pdf', pages=pages, page_count=len(reader.pages),
                    truncated=len(pages) < len(reader.pages) or any(p['truncated'] for p in pages))
    if suffix in ('.pcbdoc', '.schdoc'):
        return ole_stream_names(data)
    if b'\x00' in data[:1024] and not data.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise ReferenceError('unsupported_file')
    text = data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8', errors='replace')
    result = dict(kind='altium_project' if suffix == '.prjpcb' else 'text',
                  text=text[start:start + MAX_TEXT], truncated=len(text) > start + MAX_TEXT)
    if suffix == '.prjpcb':
        result['documents'] = [line.partition('=')[2].strip() for line in text.splitlines()
                               if line.casefold().startswith('documentpath=')][:MAX_ENTRIES]
    return result


def read_reference(arguments, *, roots=None, protected=True):
    offset = arguments.get('offset', 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ReferenceError('arguments_unusable')
    facts = facts_at(arguments, offset, roots, protected)
    # Only text continues from an offset; elsewhere it would silently be ignored.
    if offset and facts['kind'] not in ('text', 'altium_project'):
        raise ReferenceError('arguments_unusable')
    return facts


def facts_at(arguments, offset, roots, protected):
    path = reference_path(arguments['path'], roots=roots, protected=protected)
    placeholder = path.with_name('.' + path.name + '.icloud')
    if path.name.endswith('.icloud') or (not path.exists() and placeholder.is_file()):
        return {'kind': 'not_downloaded', 'path': str(path)}
    if path.is_dir():
        entries = []
        truncated = False
        scanned = 0
        for child in path.iterdir():
            scanned += 1
            if scanned > 2000:
                truncated = True
                break
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            try:
                safe = reference_path(child, roots=roots, protected=protected)
            except ReferenceError:
                continue
            entries.append({'name': child.name, 'kind': 'folder' if safe.is_dir() else 'file'})
        return dict(kind='folder', entries=entries, truncated=truncated)
    data = read_bytes(path)
    if path.suffix.casefold() == '.zip':
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            entries = archive_entries(bundle)
            selected = arguments.get('entry')
            if selected is not None:
                if selected not in {e.filename for e in entries if not e.is_dir()}:
                    raise ReferenceError('arguments_unusable')
                return dict(content_facts(selected, bundle.read(selected), offset), entry=selected)
            return dict(kind='zip', entries=[{'name': e.filename, 'bytes': e.file_size}
                                             for e in entries[:MAX_ENTRIES]],
                        truncated=len(entries) > MAX_ENTRIES)
    if 'entry' in arguments:
        raise ReferenceError('arguments_unusable')
    return content_facts(path.name, data, offset)
