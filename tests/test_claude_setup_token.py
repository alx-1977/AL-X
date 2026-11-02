"""OAuth profile validation and login fallback, without real credentials or network."""

import json
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
from alx.config import RuntimeSettings
from alx.contracts.diagnostics import mask_credentials, service_message
from alx.providers import claude_subscription as subscription
from alx.providers.claude_subscription import (
    ClaudeSubscriptionReasoningModel, ClaudeSubscriptionResearchModel,
    SubscriptionIdentityError,
)
from alx.providers.claude_usage import ClaudeUsageReader


def sentinel():
    return uuid4().hex


@pytest.fixture(autouse=True)
def profile_http(monkeypatch):
    monkeypatch.setattr(subscription, '_setup_token_profile', None)
    monkeypatch.setattr(subscription, '_setup_token_notice_taken', False)
    with patch.object(subscription.http.client, 'HTTPSConnection') as factory:
        response = factory.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({
            'account': {'email': ACCOUNT},
            'organization': {'organization_type': 'claude_team'},
        }).encode()
        yield factory, response


def model_at(tmp_path, token, model_type=ClaudeSubscriptionReasoningModel):
    runner = Runner(status(str(tmp_path)))
    return model_type('model', 30, runner=runner, config_dir=str(tmp_path),
                      expected_account=ACCOUNT, oauth_token=token, environment={
                          'CLAUDE_CODE_OAUTH_TOKEN': sentinel(),
                          'ANTHROPIC_API_KEY': sentinel(),
                          'ANTHROPIC_AUTH_TOKEN': sentinel(),
                          'ANTHROPIC_BASE_URL': 'https://invalid.test',
                          'HTTPS_PROXY': 'https://invalid.test',
                      }), runner


@pytest.mark.parametrize('plan', ['claude_pro', 'claude_max', 'claude_team'])
@pytest.mark.parametrize('model_type', [ClaudeSubscriptionReasoningModel,
                                       ClaudeSubscriptionResearchModel])
def test_profile_accepts_only_dedicated_subscription(tmp_path, profile_http, plan, model_type):
    factory, response = profile_http
    response.read.return_value = json.dumps({
        'account': {'email': ACCOUNT}, 'organization': {'organization_type': plan},
    }).encode()
    token = sentinel()
    model, runner = model_at(tmp_path, token, model_type)
    assert model.verify_identity().subscription_type == plan
    model.complete(request())
    model.verify_identity()
    assert len(runner.calls) == 1  # No CLI auth-status check of a setup token.
    factory.assert_called_once_with('api.anthropic.com', timeout=10)
    connection = factory.return_value
    connection.set_debuglevel.assert_called_once_with(0)
    assert connection.request.call_count == 1
    args, kwargs = connection.request.call_args
    assert args == ('GET', '/api/oauth/profile')
    headers = kwargs['headers']
    assert bool(headers.pop('Authorization') == 'Bearer ' + token), 'wrong credential'
    assert headers == {'User-Agent': 'claude-code/2.1.316',
                       'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}
    assert bool(runner.calls[0]['env'].get('CLAUDE_CODE_OAUTH_TOKEN') == token), 'wrong credential'
    assert model.take_setup_token_notice() is None
    connection.close.assert_called_once()


@pytest.mark.parametrize('bad_profile', [
    {}, [], None, {'account': {}, 'organization': {}},
    {'account': {'email': ACCOUNT}},
    {'account': {'email': ACCOUNT}, 'organization': {'organization_type': None}},
    {'account': {'email': 'ALX@fire-fli.co.za'}, 'organization': {'organization_type': 'claude_pro'}},
    {'account': {'email': ACCOUNT + ' '}, 'organization': {'organization_type': 'claude_pro'}},
    {'account': {'email': 'other@example.test'}, 'organization': {'organization_type': 'claude_max'}},
    *[{'account': {'email': ACCOUNT}, 'organization': {'organization_type': plan}}
      for plan in ['api', 'console', 'claude_free', 'unknown', 'claude_enterprise']],
    {'account': {'email': ACCOUNT}, 'organization': {
        'organization_type': 'claude_team', 'billing_type': 'usage_based'}},
])
def test_rejected_profiles_use_saved_login_once_notice(tmp_path, profile_http, bad_profile, caplog):
    factory, response = profile_http
    response.read.return_value = json.dumps(bad_profile).encode()
    token = sentinel()
    model, runner = model_at(tmp_path, token)
    assert model.verify_identity().account == ACCOUNT  # The normal CLI login.
    model.complete(request())
    other, _ = model_at(tmp_path, token, ClaudeSubscriptionResearchModel)
    other.verify_identity()
    assert factory.call_count == 1
    for call in runner.calls:
        assert set(call['env']) == {'CLAUDE_CONFIG_DIR'}
    notice = model.take_setup_token_notice()
    assert notice.startswith('The Claude setup token was refused because ')
    assert notice.endswith('the normal dedicated Claude login is being used.')
    assert other.take_setup_token_notice() is None
    assert model.take_setup_token_notice() is None
    assert token not in notice + caplog.text + repr(subscription._setup_token_profile), 'credential leaked'


@pytest.mark.parametrize('failure', ['network', 'timeout', 'bad_json', 'duplicate', 'too_large',
                                   'non200', 'redirect', 'request', 'read'])
def test_lookup_failures_never_escape_or_forward_secrets(tmp_path, profile_http, failure, caplog):
    factory, response = profile_http
    token = sentinel()
    connection = factory.return_value
    if failure == 'network':
        connection.getresponse.side_effect = OSError(token)
    elif failure == 'timeout':
        connection.getresponse.side_effect = TimeoutError(token)
    elif failure == 'bad_json':
        response.read.return_value = token.encode()
    elif failure == 'duplicate':
        response.read.return_value = (
            '{"account":{"email":"other@example.test","email":"' + ACCOUNT
            + '"},"organization":{"organization_type":"claude_pro"}}'
        ).encode()
    elif failure == 'too_large':
        response.read.return_value = b'x' * 65_537
    elif failure in {'non200', 'redirect'}:
        response.status = 401 if failure == 'non200' else 302
        response.headers = {'Location': 'https://invalid.test/' + token}
    elif failure == 'request':
        connection.request.side_effect = ValueError(token)
    else:
        response.read.side_effect = RuntimeError(token)
    model, runner = model_at(tmp_path, token)
    model.verify_identity()
    model.complete(request())
    assert factory.call_count == 1
    assert all('CLAUDE_CODE_OAUTH_TOKEN' not in call['env'] for call in runner.calls)
    notice = model.take_setup_token_notice()
    assert notice and token not in notice + caplog.text, 'credential leaked'
    assert model.take_setup_token_notice() is None
    # A subsequent local-login failure cannot retain the HTTP exception.
    runner.status_stdout = 'invalid'
    with pytest.raises(SubscriptionIdentityError) as raised:
        model.verify_identity()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert token not in ''.join(traceback.format_exception(raised.value)), 'credential leaked'


def test_no_token_does_not_lookup_or_change_login(tmp_path, profile_http):
    model, runner = model_at(tmp_path, None)
    model.complete(request())
    assert len(runner.calls) == 2
    profile_http[0].assert_not_called()
    assert model.take_setup_token_notice() is None
    assert all(set(call['env']) == {'CLAUDE_CONFIG_DIR'} for call in runner.calls)


@pytest.mark.parametrize('accepted', [True, False])
def test_dotenv_and_all_transports_share_startup_validation(tmp_path, profile_http, accepted):
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
    assert token not in repr(settings), 'settings exposed credential'
    with patch('alx.bootstrap.providers.subscription_cli_present', return_value=True):
        providers = build_runtime_providers(settings)
    if not accepted:
        profile_http[1].status = 403
        Path(settings.core_claude_config_dir).mkdir()
        for model in (providers.reasoning, providers.autonomous):
            model._runner = Runner(status(settings.core_claude_config_dir))
    verify_core_claude_identity(providers)
    research = _transport(settings.research.survey, None, settings.research.claude_identity,
                          settings.research.claude_oauth_token)
    for model in (providers.reasoning, providers.autonomous, research):
        environment = model.child_environment()
        if accepted:
            assert bool(environment.get('CLAUDE_CODE_OAUTH_TOKEN') == token), 'wrong credential'
        else:
            assert 'CLAUDE_CODE_OAUTH_TOKEN' not in environment
    usage = ClaudeUsageReader(providers.reasoning.executable, providers.reasoning.child_environment())
    if accepted:
        assert bool(usage.environment.get('CLAUDE_CODE_OAUTH_TOKEN') == token), 'wrong credential'
    else:
        assert 'CLAUDE_CODE_OAUTH_TOKEN' not in usage.environment
        from alx.contracts import ReasoningContext
        from alx.core.model_reasoner import _context_payload
        notice = providers.reasoning.take_setup_token_notice()
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=None, turns=(), capabilities=(),
            conversation_id='token-refusal',
            runtime={'authentication_notice': notice},
        )))
        assert payload['runtime']['authentication_notice'] == notice
        assert providers.autonomous.take_setup_token_notice() is None
    assert profile_http[0].call_count == 1


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
