"""Setup-token plumbing and fail-closed identity, without real credentials."""

import json
import subprocess
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from test_core_claude_identity import ACCOUNT, Runner, request, status
from test_subscription_autonomy import BASE_ENVIRONMENT
from alx.bootstrap.live_voice import load_environment
from alx.bootstrap.providers import build_runtime_providers, verify_core_claude_identity
from alx.bootstrap.research import _transport
from alx.config import ConfigurationError, RuntimeSettings
from alx.contracts.diagnostics import mask_credentials, service_message
from alx.providers.claude_subscription import (
    ClaudeSubscriptionReasoningModel, ClaudeSubscriptionResearchModel,
    SubscriptionIdentityError,
)
from alx.providers.claude_usage import ClaudeUsageReader
from alx.providers.errors import ProviderError


def sentinel():
    # Noncredential, generated in memory, never written into a fixture or file.
    return uuid4().hex


def test_dotenv_setting_reaches_all_subscription_transports(tmp_path):
    token = sentinel()
    with patch.object(Path, 'is_file', return_value=True), patch.object(
        Path, 'read_text', return_value='CLAUDE_CODE_OAUTH_TOKEN=' + token
    ):
        environment = load_environment(Path('unused'), inherited=BASE_ENVIRONMENT)
    settings = RuntimeSettings.from_environment({
        **environment, 'HOME': str(tmp_path), 'ALX_CLAUDE_ACCOUNT': ACCOUNT,
        'ALX_AUTONOMOUS_PROVIDER': 'claude_subscription',
        'ALX_AUTONOMOUS_MODEL': environment['ALX_REASONING_MODEL'],
        'ALX_RESEARCH_ENABLED_TIERS': 'survey',
        'ALX_RESEARCH_SURVEY_PROVIDER': 'claude_subscription',
        'ALX_RESEARCH_SURVEY_MODEL': environment['ALX_REASONING_MODEL'],
        'ALX_RESEARCH_DAILY_CALLS': '10',
    })
    assert token not in repr(settings), 'settings repr exposed the sentinel'
    with patch('alx.bootstrap.providers.subscription_cli_present', return_value=True):
        providers = build_runtime_providers(settings)
    research = _transport(settings.research.survey, None, settings.research.claude_identity,
                          settings.research.claude_oauth_token)
    for model in (providers.reasoning, providers.autonomous, research):
        assert model.child_environment().get('CLAUDE_CODE_OAUTH_TOKEN') == token, (
            'configured credential did not reach a subscription transport')
    usage = ClaudeUsageReader(providers.reasoning.executable,
                              providers.reasoning.child_environment())
    assert usage.environment.get('CLAUDE_CODE_OAUTH_TOKEN') == token, (
        'usage did not receive the shared environment')
    Path(settings.core_claude_config_dir).mkdir()
    providers.reasoning._runner = Runner(json.dumps({
        'loggedIn': True, 'authMethod': 'oauth_token', 'apiProvider': 'firstParty',
    }))
    with pytest.raises(ConfigurationError, match='subscription_token_identity_unverifiable'):
        verify_core_claude_identity(providers)


@pytest.mark.parametrize('model_type', [ClaudeSubscriptionReasoningModel,
                                       ClaudeSubscriptionResearchModel])
@pytest.mark.parametrize('configured', [False, True])
def test_environment_and_identity_fail_closed(tmp_path, model_type, configured, caplog):
    directory = tmp_path / 'dedicated'
    directory.mkdir()
    token = sentinel() if configured else None
    runner = Runner(json.dumps({
        'loggedIn': True, 'authMethod': 'oauth_token', 'apiProvider': 'firstParty',
        'configDirectory': str(directory),
    }) if configured else status(str(directory)))
    model = model_type('model', 60, runner=runner, config_dir=str(directory),
                       expected_account=ACCOUNT, oauth_token=token, environment={
        'HOME': str(tmp_path), 'CLAUDE_CODE_OAUTH_TOKEN': sentinel(),
        'ANTHROPIC_API_KEY': sentinel(), 'ANTHROPIC_AUTH_TOKEN': sentinel(),
        'ANTHROPIC_BASE_URL': 'https://invalid.test',
        'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_USE_VERTEX': '1',
        'CLAUDE_CODE_USE_FOUNDRY': '1', 'ANTHROPIC_CUSTOM_HEADERS': sentinel(),
        'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR': '4',
        'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR': '5',
    })
    if configured:
        with pytest.raises(ProviderError, match='subscription_token_identity_unverifiable'):
            model.complete(request())
        assert len(runner.calls) == 1, 'token identity refusal must precede reasoning'
        assert token not in caplog.text, 'credential leaked to logs'
    else:
        model.complete(request())
        assert len(runner.calls) == 2
    for call in runner.calls:
        env = call['env']
        assert set(env) <= {'HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN'}
        if configured:
            assert env.get('CLAUDE_CODE_OAUTH_TOKEN') == token, 'token not forwarded'
        else:
            assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env


@pytest.mark.parametrize('method', ['oauth_token', 'claude.ai', 'api_key', 'apiKey'])
@pytest.mark.parametrize('account', [ACCOUNT, 'someone@example.test'])
def test_saved_identity_or_fabricated_fields_cannot_validate_token(tmp_path, method, account):
    runner = Runner(status(str(tmp_path), authMethod=method, email=account))
    model = ClaudeSubscriptionReasoningModel(
        'model', 30, runner=runner, config_dir=str(tmp_path), expected_account=ACCOUNT,
        oauth_token=sentinel(), environment={},
    )
    with pytest.raises(SubscriptionIdentityError):
        model.verify_identity()


@pytest.mark.parametrize('failure', ['invalid_json', 'timeout'])
def test_identity_errors_retain_no_secret_exception_context(tmp_path, failure):
    token = sentinel()

    def run(command, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 30, output=token, stderr=token)
        return subprocess.CompletedProcess(command, 0, token, token)

    model = ClaudeSubscriptionReasoningModel(
        'model', 30, runner=run, config_dir=str(tmp_path), expected_account=ACCOUNT,
        oauth_token=token, environment={},
    )
    with pytest.raises(SubscriptionIdentityError) as raised:
        model.verify_identity()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert token not in ''.join(traceback.format_exception(raised.value)), 'trace leaked credential'


def test_named_oauth_setting_is_masked_even_for_short_noncredential_sentinels():
    token = sentinel()
    for text in ('CLAUDE_CODE_OAUTH_TOKEN=' + token,
                 json.dumps({'CLAUDE_CODE_OAUTH_TOKEN': token})):
        for masked in (mask_credentials(text), service_message(text)):
            assert token not in masked, 'credential assignment leaked'
            assert '[masked]' in masked
    shaped = 'sk-ant-oat01-' + sentinel()
    assert shaped not in mask_credentials(shaped), 'unlabelled OAuth shape leaked'


@pytest.mark.parametrize('redraw', [False, True])
def test_usage_masks_unlabelled_token_before_evidence_is_returned(redraw):
    token = sentinel()
    reader = ClaudeUsageReader('unused', {'CLAUDE_CODE_OAUTH_TOKEN': token})
    echoed = token[:16] + '\x1b[m' + token[16:] if redraw else token
    # A CLI can echo arbitrary text even before it draws its usage screen.
    @contextmanager
    def terminal(*args, **kwargs):
        assert kwargs['env'].get('CLAUDE_CODE_OAUTH_TOKEN') == token, 'token not forwarded'
        yield 7, object()

    with patch('alx.providers.claude_usage.interactive_terminal', side_effect=terminal), patch(
        'alx.providers.claude_usage.select.select', return_value=([7], [], [])
    ), patch(
        'alx.providers.claude_usage.os.read', side_effect=[echoed.encode(), b'']
    ):
        result = reader.read()
    assert token not in json.dumps(result), 'usage failure leaked credential'
    assert '[masked]' in result['screen_capture']
    if not redraw:
        assert '[masked]' in result['raw_output_tail']
