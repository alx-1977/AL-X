"""D-062: real local processes, fake toolchains/CLI, and a local bare remote."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from alx.bootstrap.particle_local import build_particle_local_runtime
from alx.config.settings import KiCadSettings, ParticleFirmwareSettings
from alx.contracts import CapabilityResultState, SideEffect, ModelCompletion, CapabilityAttemptDisposition
from alx.contracts.coding import CodingCommit, CodingError, CodingOutcome, CodingRequest, CodingSessionResult
from alx.providers.coding_agent import CodingAgent
from alx.providers.coding_process import CodingCancellation, bind_cancellation, reset_cancellation
from alx.providers.firmware_git import prepare_firmware_clone, push_firmware_branch
from alx.providers.particle_local import ParticleLocal, ParticleLocalError, bounded_process
from alx.tools.coding import DEFINITION, parse_coding_arguments, goal_coding_branch
from alx.tools.particle_local import DEFINITIONS, build_particle_local_executors

REPO = 'alx-1977/RoomReader_V2'
DEVICE = '0123456789abcdef01234567'


def executable(path, source):
    path.write_text('#!' + sys.executable + '\n' + source)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def local(tmp_path, monkeypatch):
    root = tmp_path / 'data' / 'firmware'
    source = root / 'repositories' / 'alx-1977--RoomReader_V2'
    source.mkdir(parents=True)
    (source / 'main.cpp').write_text('void setup() {}\n')
    tools = tmp_path / 'toolchains'
    for directory in ('gcc-arm/10.2.1/bin', 'buildtools/1.1.1/bin',
                      'deviceOS/6.4.1/platform/MCU', 'deviceOS/6.4.1/build', 'buildscripts/1.17.2'):
        (tools / directory).mkdir(parents=True)
    (tools / 'deviceOS/6.4.1/build/platform-id.mk').write_text('PLATFORM_B5SOM = 35\n')
    (tools / 'buildscripts/1.17.2/Makefile').write_text('# fake toolchain\n')
    executable(tools / 'gcc-arm/10.2.1/bin/arm-none-eabi-gcc', 'pass\n')
    make = executable(tmp_path / 'make', '''import json, os, sys
from pathlib import Path
record = {'argv': sys.argv[1:], 'path': os.environ['PATH']}
Path(os.environ['BUILD_RECORD']).write_text(json.dumps(record))
app = Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('APPDIR=')))
print('a full log line that must not be returned')
print('main.cpp:3:4: warning: sample')
if os.environ.get('BUILD_MANY'):
    for n in range(200):
        print('main.cpp:12: error: bounded failure ' + str(n))
        print('main.cpp:1: warning: bounded warning')
    sys.exit(1)
if os.environ.get('BUILD_FAIL'):
    print('main.cpp:5:2: error: missing value')
    print('ld: undefined reference to setup')
    sys.exit(1)
if not os.environ.get('NO_BINARY'):
    (app / 'target').mkdir(exist_ok=True)
    (app / 'target/app.bin').write_bytes(b'firmware')
''')
    particle = executable(tmp_path / 'particle', '''import os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ['usb', 'list']:
    print(os.environ.get('USB_LINES', 'bench [0123456789abcdef01234567] (B5 SoM, normal)'))
    sys.exit(int(os.environ.get('USB_EXIT', '0')))
if args[:2] == ['flash', '--local'] and len(args) == 3:
    Path(os.environ['FLASH_RECORD']).write_text(repr(args))
    sys.exit(int(os.environ.get('FLASH_EXIT', '0')))
if args == ['serial', 'monitor', '--follow']:
    print('serial-data' * 5000, flush=True)
    time.sleep(30)
else:
    sys.exit(90)
''')
    monkeypatch.setenv('BUILD_RECORD', str(tmp_path / 'build.json'))
    monkeypatch.setenv('FLASH_RECORD', str(tmp_path / 'flash.txt'))
    return ParticleLocal(particle, tools, root, (REPO,), make=make)


def test_compile_arguments_filtered_results_and_snapshot(local):
    result = local.compile(REPO)
    assert set(result) == {'succeeded', 'errors', 'warning_count', 'binary', 'size', 'duration_seconds'}
    assert result['succeeded'] and result['errors'] == [] and result['warning_count'] == 1
    assert result['size'] == 8 and Path(result['binary']).read_bytes() == b'firmware'
    record = json.loads(Path(os.environ['BUILD_RECORD']).read_text())
    assert record['argv'][:3] == ['-f', str(local.toolchains / 'buildscripts/1.17.2/Makefile'), 'compile-user']
    assert 'PLATFORM=b5som' in record['argv']
    assert 'DEVICE_OS_PATH=' + str(local.toolchains / 'deviceOS/6.4.1') in record['argv']
    assert record['path'].split(os.pathsep)[:2] == [str(local.toolchains / 'gcc-arm/10.2.1/bin'),
                                                               str(local.toolchains / 'buildtools/1.1.1/bin')]
    assert list(local.source(REPO).rglob('*.bin')) == []
    assert list((local.root / 'builds').iterdir()) == []


def test_failed_compile_only_returns_errors(local, monkeypatch):
    monkeypatch.setenv('BUILD_FAIL', '1')
    result = local.compile(REPO)
    assert not result['succeeded'] and not result['binary']
    assert result['errors'][0] == {'file': 'main.cpp', 'line': 5, 'message': 'missing value'}
    assert len(result['errors']) == 2 and result['warning_count'] == 1
    assert 'full log' not in json.dumps(result)


@pytest.mark.parametrize('revision', ['HEAD~1', 'HEAD^', 'origin/main', 'a' * 40])
def test_well_formed_revision_mismatch_is_not_malformed(local, revision):
    with patch('alx.providers.particle_local.bounded_process',
               side_effect=[(0, 'a' * 40, False, False),
                            (0, 'b' * 40, False, False)]) as runner:
        with pytest.raises(ParticleLocalError, match='^revision_mismatch$'):
            local.source(REPO, revision=revision)
    assert runner.call_args_list[0].args[0] == [
        'git', 'rev-parse', '--verify', revision + '^{commit}']


@pytest.mark.parametrize('revision', ['', '--help', 'HEAD;echo', 'HEAD\n', 1])
def test_malformed_revision_never_runs_git(local, revision):
    with patch('alx.providers.particle_local.bounded_process') as runner:
        with pytest.raises(ParticleLocalError, match='^arguments_unusable$'):
            local.source(REPO, revision=revision)
    runner.assert_not_called()


def test_compile_diagnostic_bounds(local, monkeypatch):
    monkeypatch.setenv('BUILD_MANY', '1')
    result = local.compile(REPO)
    assert len(result['errors']) == 40 and result['warning_count'] == 201
    assert not result['succeeded']


def test_local_executor_contracts_and_failed_enumeration(local, monkeypatch):
    executors = build_particle_local_executors(local, lambda: 'local-call')
    result = executors['compile_particle_firmware']({'repository': REPO})
    assert result.state is CapabilityResultState.SUCCEEDED
    assert DEFINITIONS[0].output_schema.accepts(result.values)
    refused = executors['compile_particle_firmware']({'repository': REPO, 'task': 'raw text'})
    assert refused.failure['code'] == 'arguments_unusable'
    monkeypatch.setenv('USB_EXIT', '1')
    result = executors['flash_bench_reader_usb']({'binary': result.values['binary']})
    assert result.state is CapabilityResultState.FAILED and result.failure['code'] == 'usb_list_failed'
    assert not Path(os.environ['FLASH_RECORD']).exists()


def test_stale_binary_is_never_certified(local, monkeypatch):
    (local.source(REPO) / 'stale.bin').write_bytes(b'stale')
    monkeypatch.setenv('NO_BINARY', '1')
    result = local.compile(REPO)
    assert not result['succeeded'] and not result['binary']


@pytest.mark.parametrize('arguments,code', [({'repository': 'elsewhere/repo'}, 'repository_denied'),
    ({'repository': REPO, 'platform': '../x'}, 'arguments_unusable'),
    ({'repository': REPO, 'device_os': '9.0'}, 'toolchain_unavailable'),
    ({'repository': REPO, 'platform': 'argon'}, 'platform_unavailable')])
def test_compile_validation(local, arguments, code):
    with pytest.raises(ParticleLocalError, match=code):
        local.compile(**arguments)


def test_usb_devices_and_successful_local_flash_survive_restart(local):
    assert local.usb_devices()['devices'] == [dict(device_id=DEVICE, name='bench', platform='B5 SoM', mode='normal')]
    artifact = local.compile(REPO)['binary']
    restarted = ParticleLocal(local.cli, local.toolchains, local.root, (REPO,))
    assert restarted.flash(artifact, DEVICE)['outcome'] == 'succeeded'
    assert "'--local'" in Path(os.environ['FLASH_RECORD']).read_text()


@pytest.mark.parametrize('lines,code', [('', 'usb_device_count'),
    ('No devices found.', 'usb_device_count'),
    ('bench [0123456789abcdef01234567] (b5som, normal)\nother [aaaaaaaaaaaaaaaaaaaaaaaa] (b5som, DFU)', 'usb_device_count'),
    ('unreadable data', 'usb_list_failed')])
def test_flash_refuses_unsafe_usb_listing(local, monkeypatch, lines, code):
    artifact = local.compile(REPO)['binary']
    monkeypatch.setenv('USB_LINES', lines)
    with pytest.raises(ParticleLocalError, match=code):
        local.flash(artifact)
    assert not Path(os.environ['FLASH_RECORD']).exists()


def test_flash_refuses_unproven_changed_or_wrong_device(local):
    unproven = local.source(REPO) / 'anything.bin'
    unproven.write_bytes(b'anything')
    with pytest.raises(ParticleLocalError, match='binary_untrusted'):
        local.flash(str(unproven))
    artifact = local.compile(REPO)['binary']
    with pytest.raises(ParticleLocalError, match='device_mismatch'):
        local.flash(artifact, 'a' * 24)
    Path(artifact).write_bytes(b'changed')
    with pytest.raises(ParticleLocalError, match='binary_untrusted'):
        local.flash(artifact)
    assert not Path(os.environ['FLASH_RECORD']).exists()


def test_serial_is_bounded_and_terminated(local):
    started = time.monotonic()
    result = local.serial(seconds=1)
    assert time.monotonic() - started < 3
    assert result['truncated'] and len(result['text']) <= 32768
    assert result['text'].startswith('serial-data')
    for value in (0, 61, True, '10'):
        with pytest.raises(ParticleLocalError, match='arguments_unusable'):
            local.serial(value)


def test_process_timeout_and_large_output(tmp_path):
    cli = executable(tmp_path / 'long', 'import time\nprint("x" * 100000, flush=True)\ntime.sleep(30)\n')
    code, output, clipped, timeout = bounded_process([cli], seconds=2, limit=100)
    assert code != 0 and timeout and clipped and len(output) == 100


def test_catalogue_and_settings(tmp_path):
    runtime = build_particle_local_runtime(lambda: 'c', {}, tmp_path)
    ids = {d.capability_id for d in runtime.definitions}
    assert ids == {'compile_particle_firmware', 'list_particle_usb_devices', 'flash_bench_reader_usb', 'read_particle_serial_log'}
    assert ids == set(runtime.executors) == set(runtime.policies)
    assert [d.side_effect for d in DEFINITIONS] == [SideEffect.NONE, SideEffect.NONE, SideEffect.EFFECTFUL, SideEffect.NONE]
    assert not runtime.policies['flash_bench_reader_usb'].approval_required
    assert runtime.settings.cli == 'particle'
    assert runtime.settings.repositories == (REPO,)
    configured = ParticleFirmwareSettings.from_environment({'ALX_DATA_DIRECTORY': str(tmp_path),
        'ALX_PARTICLE_CLI': '/configured/particle', 'ALX_PARTICLE_TOOLCHAIN_ROOT': str(tmp_path / 'tools')}, tmp_path)
    assert configured.root == tmp_path / 'firmware' and configured.cli == '/configured/particle'
    roots = KiCadSettings.from_environment({}, tmp_path).design_roots
    icloud = Path('~/Library/Mobile Documents/com~apple~CloudDocs/ALX_Storage/Projects').expanduser().resolve()
    assert icloud in roots
    assert icloud in KiCadSettings.from_environment({'ALX_KICAD_DESIGN_ROOTS': str(tmp_path)}, tmp_path).design_roots
    assert 'firmware_repository' in DEFINITION.input_schema.properties


def test_firmware_branch_does_not_authorise_canonical_branch_continuation():
    values = {'branch': 'firmware/test', 'firmware_repository': REPO}
    attempt = SimpleNamespace(disposition=CapabilityAttemptDisposition.EXECUTED,
        implementation_invoked=True, call=SimpleNamespace(capability_id='run_coding_task', arguments={}),
        result=SimpleNamespace(recovery_values=lambda: values, recovery_failure=lambda: None))
    state = SimpleNamespace(attempts=(attempt,))
    assert goal_coding_branch(state, 'firmware/test') is None
    del values['firmware_repository']
    assert goal_coding_branch(state, 'firmware/test').branch == 'firmware/test'


def git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for key in ('GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME'):
        monkeypatch.setenv(key, 'Fixture')
    for key in ('GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.setenv(key, 'fixture@example.invalid')
    canonical = tmp_path / 'canonical'
    seed = tmp_path / 'seed'
    for root in (canonical, seed):
        root.mkdir()
        git(root, 'init', '-b', 'main')
        (root / 'main.cpp').write_text('void setup() {}\n')
        git(root, 'add', '--', 'main.cpp')
        git(root, 'commit', '-m', 'initial')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'clone', '--bare', str(seed), str(remote))
    return canonical, remote


class Planner:
    def complete(self, request):
        return ModelCompletion('fake', 'fake', dict(problem_understanding='Edit firmware',
            hypotheses=['test'], inspection_targets=['main.cpp'], intended_changes=['edit'],
            verification=['compile'], risks_constraints=['feature only'], more_context_required=False))


class Session:
    has_terminal = True
    def run_session(self, request, briefing):
        self.root = Path(request.worktree)
        self.briefing = briefing
        (self.root / 'main.cpp').write_text('void setup() { int x = 1; }\n')
        return CodingSessionResult(completed=True, report='edited', turns=1)


def test_coding_job_clones_compiles_commits_pushes_only_feature(local, repos):
    canonical, remote = repos
    original = git(canonical, 'rev-parse', 'HEAD')
    main = git(remote, 'rev-parse', 'main')
    session = Session()
    agent = CodingAgent(Planner(), session, repository=canonical, firmware_root=local.root,
                        firmware_remotes={REPO: str(remote)}, firmware_compiler=local.compile)
    request, error = parse_coding_arguments(dict(task='Update firmware', firmware_repository=REPO,
        repair_branch='firmware/test', commit_message='Update fixture'), 'firmware-job')
    assert error is None
    result = agent.run(request)
    assert result.status == 'succeeded', (result.unresolved_issues, result.diagnostics)
    assert result.commit.branch == 'firmware/test'
    assert git(remote, 'rev-parse', 'refs/heads/firmware/test') == result.commit.commit_sha
    assert git(remote, 'rev-parse', 'main') == main
    assert git(canonical, 'rev-parse', 'HEAD') == original
    assert git(canonical, 'branch', '--show-current') == 'main'
    assert session.root == local.root / 'jobs/firmware-job/source'
    assert 'particle_compile' in result.verification.required
    assert not any('pytest' in c.argv for c in result.commands)
    assert 'does not use AL/X' in session.briefing
    assert git(session.root, 'status', '--porcelain') == ''
    with pytest.raises(CodingError, match='git_refused'):
        push_firmware_branch(session.root, 'main', result.commit.commit_sha, str(remote))


def test_firmware_clone_refuses_unlisted_repo_and_storage_inside_alx(local, repos):
    canonical, remote = repos
    with pytest.raises(CodingError):
        prepare_firmware_clone(local.root, canonical, 'other/repo', {REPO: str(remote)}, 'j')
    with pytest.raises(CodingError):
        prepare_firmware_clone(canonical / 'data', canonical, REPO, {REPO: str(remote)}, 'j')
    assert git(remote, 'branch', '--list') == '* main'


def test_clone_starts_at_latest_origin_main(local, repos, tmp_path):
    canonical, remote = repos
    seed = tmp_path / 'seed'
    (seed / 'new.cpp').write_text('latest\n')
    git(seed, 'add', '--', 'new.cpp')
    git(seed, 'commit', '-m', 'latest')
    git(seed, 'push', str(remote), 'main')
    clone = prepare_firmware_clone(local.root, canonical, REPO, {REPO: str(remote)}, 'fresh')
    assert git(clone, 'rev-parse', 'HEAD') == git(remote, 'rev-parse', 'main')
    assert (clone / 'new.cpp').read_text() == 'latest\n'
    with pytest.raises(ParticleLocalError, match='revision_mismatch'):
        local.compile(REPO, job_id='fresh', revision='HEAD~1')


def test_failed_compile_preserves_clone_without_commit_or_push(local, repos, monkeypatch):
    canonical, remote = repos
    monkeypatch.setenv('BUILD_FAIL', '1')
    agent = CodingAgent(Planner(), Session(), repository=canonical, firmware_root=local.root,
                        firmware_remotes={REPO: str(remote)}, firmware_compiler=local.compile)
    result = agent.run(CodingRequest('Edit', 'failed', firmware_repository=REPO,
                                    repair_branch='firmware/fail', commit_message='Edit'))
    assert result.status == 'failed' and result.commit is None
    assert 'particle_compile' in result.verification.failed
    assert git(remote, 'branch', '--list', 'firmware/fail') == ''
    checkpoint = json.loads(result.checkpoint)
    assert checkpoint['firmware_job_id'] == 'failed'
    monkeypatch.delenv('BUILD_FAIL')
    resumed = agent.run(CodingRequest('Edit', 'retry', firmware_repository=REPO,
        repair_branch='firmware/fail', commit_message='Edit', resume_checkpoint=checkpoint))
    assert resumed.status == 'succeeded', (resumed.unresolved_issues, resumed.diagnostics)
    assert git(remote, 'rev-parse', 'firmware/fail') == resumed.commit.commit_sha


def test_failed_push_resumes_exact_commit_after_restart(local, repos):
    canonical, remote = repos
    def agent(session):
        return CodingAgent(Planner(), session, repository=canonical, firmware_root=local.root,
                           firmware_remotes={REPO: str(remote)}, firmware_compiler=local.compile)
    session = Session()
    with patch('alx.providers.firmware_git.push_firmware_branch',
               side_effect=CodingError('git_unavailable', reason_code='firmware_push_failed')):
        result = agent(session).run(CodingRequest('Edit', 'push-failed', firmware_repository=REPO,
            repair_branch='firmware/push', commit_message='Edit'))
    assert result.status == 'failed' and result.commit is not None
    checkpoint = json.loads(result.checkpoint)
    assert checkpoint['stage'] == 'commit'
    assert checkpoint['commit_candidate_sha'] == result.commit.commit_sha
    class NoSession:
        def run_session(self, *args):
            pytest.fail('push retry must never reopen a coding session')
    resumed = agent(NoSession()).run(CodingRequest('Edit', 'push-retry', firmware_repository=REPO,
        repair_branch='firmware/push', commit_message='Edit', resume_checkpoint=checkpoint))
    assert resumed.status == 'succeeded'
    assert resumed.commit.commit_sha == result.commit.commit_sha
    assert git(remote, 'rev-parse', 'firmware/push') == result.commit.commit_sha


@pytest.mark.parametrize('close_output', [False, True])
def test_stop_firmware_job_during_compile_terminates_compiler(local, repos, tmp_path, close_output):
    canonical, remote = repos
    started = tmp_path / 'compiler-started'
    finished = tmp_path / 'compiler-finished'
    local.make = executable(tmp_path / 'slow-make', f'''import os, time
from pathlib import Path
Path({str(started)!r}).write_text(str(os.getpid()))
if {close_output!r}:
    os.close(1)
    os.close(2)
time.sleep(5)
Path({str(finished)!r}).write_text('compiler ran to completion')
''')
    agent = CodingAgent(Planner(), Session(), repository=canonical, firmware_root=local.root,
                        firmware_remotes={REPO: str(remote)}, firmware_compiler=local.compile)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(agent.run, CodingRequest('Edit', 'stop-compile',
            firmware_repository=REPO, repair_branch='firmware/stop', commit_message='Edit'))
        deadline = time.monotonic() + 10
        while not started.exists() and not pending.done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert started.exists(), pending.result() if pending.done() else 'compile never started'
        assert agent.cancel('stop-compile')
        stopped_at = time.monotonic()
        result = pending.result(timeout=10)
        assert time.monotonic() - stopped_at < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), 0)
    assert not finished.exists()
    assert result.status == 'cancelled'
    assert 'coding_cancelled' in result.unresolved_issues
    assert result.verification is None or 'particle_compile' not in result.verification.failed
    assert result.commit is None
    assert git(remote, 'branch', '--list', 'firmware/stop') == ''


def test_compile_cancellation_is_not_verification_failure(tmp_path, monkeypatch):
    monkeypatch.setattr('alx.providers.coding_agent.canonical_repository_root', lambda root: root)
    from alx.providers.coding_agent import firmware_verification_checks
    checks = firmware_verification_checks()
    monkeypatch.setattr('alx.providers.coding_agent.firmware_verification_checks', lambda: checks[-1:])
    def cancelled():
        raise CodingError('coding_cancelled')
    agent = CodingAgent(Planner(), Session(), repository=tmp_path, firmware_verifier=cancelled)
    with pytest.raises(CodingError, match='^coding_cancelled$'):
        agent._verify(CodingRequest('Edit', 'cancel-verification', worktree=str(tmp_path)), None, (), [])


@pytest.mark.parametrize('close_output', [False, True])
def test_local_compile_observes_bound_job_cancellation(local, tmp_path, close_output):
    started = tmp_path / 'started'
    local.make = executable(tmp_path / 'waiting-make', f'''import os, time
from pathlib import Path
Path({str(started)!r}).write_text(str(os.getpid()))
if {close_output!r}:
    os.close(1)
    os.close(2)
time.sleep(5)
''')
    cancellation = CodingCancellation()
    def compile():
        token = bind_cancellation(cancellation)
        try:
            return local.compile(REPO)
        finally:
            reset_cancellation(token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(compile)
        deadline = time.monotonic() + 10
        while not started.exists() and not pending.done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert started.exists()
        cancellation.cancel()
        stopped_at = time.monotonic()
        with pytest.raises(CodingError, match='^coding_cancelled$'):
            pending.result(timeout=10)
        assert time.monotonic() - stopped_at < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), 0)


@pytest.mark.parametrize('push_fails', [False, True])
def test_publication_lifecycle_with_isolated_git_mechanics(tmp_path, monkeypatch, push_fails):
    monkeypatch.setattr('alx.providers.coding_agent.canonical_repository_root', lambda root: root)
    monkeypatch.setattr('alx.providers.coding_agent.coding_job_lock', lambda root: nullcontext())
    monkeypatch.setattr('alx.providers.firmware_git.prepare_firmware_clone', lambda *args: tmp_path)
    monkeypatch.setattr(CodingAgent, '_checkpoint', lambda *args: '{}')
    def run(self, request, state):
        state.branch = 'firmware/status'
        return CodingOutcome('succeeded', 'edited', (), (), False, None, '', '', (),
            datetime.now(timezone.utc), commit=CodingCommit('firmware/status', 'a' * 40, (), True))
    monkeypatch.setattr(CodingAgent, '_run', run)
    reports = []
    observed = []
    def push(*args):
        observed.extend(reports)
        if push_fails:
            raise CodingError('git_unavailable', reason_code='firmware_push_failed')
    monkeypatch.setattr('alx.providers.firmware_git.push_firmware_branch', push)
    agent = CodingAgent(Planner(), Session(), telemetry_sink=reports.append,
        repository=tmp_path, firmware_root=tmp_path,
        firmware_remotes={REPO: 'local-remote'}, firmware_compiler=lambda **kwargs: {})
    result = agent.run(CodingRequest('Edit', 'publish', firmware_repository=REPO))
    assert observed and not any(report.terminal for report in observed)
    assert observed[-1].in_flight
    terminal = [report for report in reports if report.terminal]
    assert len(terminal) == 1
    assert terminal[0].outcome == result.status == ('failed' if push_fails else 'succeeded')
    assert terminal[0].transition == ('FAILED' if push_fails else 'COMPLETE')


@pytest.mark.parametrize('push_fails', [False, True])
def test_firmware_status_stays_active_until_push_finishes(local, repos, push_fails):
    canonical, remote = repos
    reports = []
    observed_during_push = []
    def push(*args):
        observed_during_push.extend(reports)
        if push_fails:
            raise CodingError('git_unavailable', reason_code='firmware_push_failed')
        push_firmware_branch(*args)
    agent = CodingAgent(Planner(), Session(), telemetry_sink=reports.append,
        repository=canonical, firmware_root=local.root,
        firmware_remotes={REPO: str(remote)}, firmware_compiler=local.compile)
    with patch('alx.providers.firmware_git.push_firmware_branch', side_effect=push):
        result = agent.run(CodingRequest('Edit', 'publish-status', firmware_repository=REPO,
            repair_branch='firmware/status', commit_message='Edit'))
    assert observed_during_push
    assert not any(report.terminal for report in observed_during_push)
    assert observed_during_push[-1].in_flight
    terminal = [report for report in reports if report.terminal]
    assert len(terminal) == 1
    assert terminal[0].outcome == result.status == ('failed' if push_fails else 'succeeded')
    assert terminal[0].transition == ('FAILED' if push_fails else 'COMPLETE')
