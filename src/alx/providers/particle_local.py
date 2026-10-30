"""D-062 local Particle mechanics. No cloud access or conversational policy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from alx.providers.coding_process import check_cancelled


class ParticleLocalError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def bounded_process(argv, *, seconds, cwd=None, env=None, limit=32768, on_line=None):
    """Drain continuously, retain bounded bytes, reap the entire process group."""
    check_cancelled()
    started = time.monotonic()
    output = bytearray()
    pending = b''
    truncated = False
    timed_out = False
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        raise ParticleLocalError('cli_unavailable') from error
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                check_cancelled()
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, limit - len(output))
                    output.extend(chunk[:available])
                    truncated |= len(chunk) > available
                    if on_line:
                        pending += chunk
                        lines = pending.split(b'\n')
                        pending = lines.pop()[-4096:]
                        for line in lines:
                            on_line(line[:4096].decode('utf-8', errors='replace'))
            if pending and on_line:
                on_line(pending.decode('utf-8', errors='replace'))
        # A tool may close its output before exiting. Keep observing the same
        # job cancellation while waiting, with or without an open pipe.
        while not timed_out:
            check_cancelled()
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(remaining, .1))
                check_cancelled()
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        # Also stop any descendant keeping a serial port or pipe open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
    check_cancelled()
    return process.returncode, output.decode('utf-8', errors='replace'), truncated, timed_out


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            value.update(chunk)
    return value.hexdigest()


class ParticleLocal:
    def __init__(self, cli, toolchains, root, repositories, *, make='make'):
        self.cli = cli
        self.toolchains = Path(toolchains).resolve()
        self.root = Path(root).resolve()
        self.repositories = tuple(repositories)
        self.make = make

    def source(self, repository, job_id=None, revision=None):
        if repository not in self.repositories:
            raise ParticleLocalError('repository_denied')
        base = self.root / 'repositories' / repository.replace('/', '--')
        if job_id is not None:
            if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
                raise ParticleLocalError('arguments_unusable')
            base = self.root / 'jobs' / job_id
            try:
                if json.loads((base / 'repository.json').read_text()) != repository:
                    raise ParticleLocalError('repository_denied')
            except (OSError, ValueError) as error:
                raise ParticleLocalError('repository_denied') from error
            base = base / 'source'
        resolved = base.resolve()
        if resolved != base or not resolved.is_relative_to(self.root) or not resolved.is_dir():
            raise ParticleLocalError('repository_denied')
        if revision is not None:
            if not isinstance(revision, str) or not re.fullmatch(r'[A-Za-z0-9_./~^-]{1,200}', revision) or revision.startswith('-'):
                raise ParticleLocalError('arguments_unusable')
            def rev(value):
                from alx.providers.coding_git import _clean_environment
                code, out, clipped, timeout = bounded_process(
                    ['git', 'rev-parse', '--verify', value + '^{commit}'], seconds=10,
                    cwd=resolved, env=_clean_environment())
                if code or clipped or timeout:
                    raise ParticleLocalError('revision_mismatch')
                return out.strip()
            if rev(revision) != rev('HEAD'):
                raise ParticleLocalError('revision_mismatch')
        return resolved

    def compile(self, repository, job_id=None, revision=None, platform='b5som',
                device_os='6.4.1', buildscripts='1.17.2'):
        source = self.source(repository, job_id, revision)
        builds = self.root / 'builds'
        builds.mkdir(parents=True, exist_ok=True)
        # Build a snapshot: generated target files must never enter a coding commit.
        with tempfile.TemporaryDirectory(dir=builds) as temporary:
            snapshot = Path(temporary) / 'source'
            shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns('.git', 'target', '*.bin'),
                            symlinks=True)
            # A build must not follow source links into another project or credentials.
            if any(p.is_symlink() for p in snapshot.rglob('*')):
                raise ParticleLocalError('repository_denied')
            return self._compile_snapshot(snapshot, platform, device_os, buildscripts)

    def _compile_snapshot(self, source, platform, device_os, buildscripts):
        started = time.monotonic()
        for value in (platform, device_os, buildscripts):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) or value in {'.', '..'}:
                raise ParticleLocalError('arguments_unusable')
        os_root = self.toolchains / 'deviceOS' / device_os
        makefile = self.toolchains / 'buildscripts' / buildscripts / 'Makefile'
        gcc = self.toolchains / 'gcc-arm' / '10.2.1' / 'bin'
        buildtools = sorted((p for p in (self.toolchains / 'buildtools').glob('*/bin')
                             if p.is_dir() and re.fullmatch(r'\d+(?:\.\d+)*', p.parent.name)),
                            key=lambda p: tuple(int(n) for n in p.parent.name.split('.')))
        # Device OS declares the available platform directories.
        if not (os_root / 'platform' / 'MCU').is_dir() or not makefile.is_file() or not os.access(gcc / 'arm-none-eabi-gcc', os.X_OK) or not buildtools:
            raise ParticleLocalError('toolchain_unavailable')
        platform_files = list((os_root / 'build').glob('*.mk')) + list((os_root / 'platform').rglob('*.mk'))
        if not platform_files or not any(platform.lower() in re.split(r'[^a-z0-9]+', p.read_text(errors='replace').lower()) for p in platform_files):
            raise ParticleLocalError('platform_unavailable')
        before = {str(p.resolve()): (p.stat().st_mtime_ns, digest(p))
                  for p in source.rglob('*.bin') if p.is_file()}
        errors = []
        warnings = 0
        def line(diagnostic_line):
            nonlocal warnings
            warnings += len(re.findall(r'\bwarning:', diagnostic_line))
            if len(errors) >= 40:
                return
            match = re.match(r'(.+?):(\d+)(?::\d+)?:\s*(?:fatal )?error:\s*(.*)', diagnostic_line)
            if match:
                errors.append({'file': match[1][-512:], 'line': int(match[2]), 'message': match[3][:1000]})
            elif any(piece in diagnostic_line for piece in ('undefined reference', 'collect2: error:', 'ld: ', 'Error ', 'error:')):
                errors.append({'file': '', 'line': 0, 'message': diagnostic_line[:1000]})
        env = dict(os.environ)
        env['PATH'] = os.pathsep.join((str(gcc), str(buildtools[-1]), env.get('PATH', '')))
        code, _, _, timeout = bounded_process([
            self.make, '-f', str(makefile), 'compile-user', 'DEVICE_OS_PATH=' + str(os_root),
            'PLATFORM=' + platform, 'APPDIR=' + str(source)],
            seconds=600, cwd=source, env=env, limit=0, on_line=line)
        candidates = [p for p in source.rglob('*.bin') if p.is_file() and not p.is_symlink()
                      and p.resolve().is_relative_to(source)
                      and before.get(str(p.resolve())) != (p.stat().st_mtime_ns, digest(p))]
        succeeded = code == 0 and not timeout and len(candidates) == 1 and 0 < candidates[0].stat().st_size <= 16 * 1024 * 1024
        binary = ''
        size = 0
        if succeeded:
            # Immutable artifact copy separates compiler output from later source builds.
            artifacts = self.root / 'artifacts'
            artifacts.mkdir(parents=True, exist_ok=True)
            artifact = artifacts / (uuid4().hex + '.bin')
            artifact.write_bytes(candidates[0].read_bytes())
            binary, size = str(artifact), artifact.stat().st_size
            artifact.with_suffix('.json').write_text(json.dumps({'path': binary, 'sha256': digest(artifact)}))
        elif not errors:
            errors.append({'file': '', 'line': 0, 'message': 'compile_timeout' if timeout else
                           'binary_missing_or_ambiguous' if code == 0 else 'compile_failed'})
        return {'succeeded': succeeded, 'errors': errors, 'warning_count': warnings,
                'binary': binary, 'size': size, 'duration_seconds': time.monotonic() - started}

    def usb_devices(self):
        code, output, clipped, timeout = bounded_process([self.cli, 'usb', 'list'], seconds=15)
        if code or clipped or timeout:
            raise ParticleLocalError('usb_list_failed')
        devices = []
        # Particle CLI: '<name> [<24 hex id>] (<platform>, <mode>)'.
        for line in output.splitlines():
            if not line.strip() or line.strip() == 'No devices found.':
                continue
            match = re.fullmatch(r'\s*(.*?)\s*\[([0-9a-fA-F]{24})\]\s*\(([^,()]+)(?:,\s*([^()]+))?\)\s*', line)
            if not match:
                raise ParticleLocalError('usb_list_failed')
            devices.append({'device_id': match[2].lower(), 'name': match[1],
                            'platform': match[3].strip(), 'mode': (match[4] or 'unknown').strip()})
        if len({d['device_id'] for d in devices}) != len(devices):
            raise ParticleLocalError('usb_list_failed')
        return {'devices': devices}

    def single_device(self, expected_device_id=None):
        devices = self.usb_devices()['devices']
        if len(devices) != 1:
            raise ParticleLocalError('usb_device_count')
        device = devices[0]['device_id']
        if expected_device_id is not None and expected_device_id != device:
            raise ParticleLocalError('device_mismatch')
        return device

    def flash(self, binary, expected_device_id=None):
        path = Path(binary).resolve()
        try:
            if path.parent != self.root / 'artifacts' or path.suffix != '.bin' or path.is_symlink():
                raise ParticleLocalError('binary_untrusted')
            record = json.loads(path.with_suffix('.json').read_text())
            if record != {'path': str(path), 'sha256': digest(path)}:
                raise ParticleLocalError('binary_untrusted')
        except (OSError, ValueError, TypeError) as error:
            raise ParticleLocalError('binary_untrusted') from error
        device = self.single_device(expected_device_id)
        code, output, _, timeout = bounded_process([self.cli, 'flash', '--local', str(path)], seconds=120, limit=2000)
        return {'device_id': device, 'binary': str(path), 'outcome': 'unknown' if timeout else
                'succeeded' if code == 0 else 'failed', 'error': output if code or timeout else ''}

    def serial(self, seconds=10, expected_device_id=None):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 60:
            raise ParticleLocalError('arguments_unusable')
        self.single_device(expected_device_id)
        code, output, clipped, timeout = bounded_process(
            [self.cli, 'serial', 'monitor', '--follow'], seconds=seconds, limit=32768)
        if code and not timeout:
            raise ParticleLocalError('serial_failed')
        return {'text': output, 'truncated': clipped}
