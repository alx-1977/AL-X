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


def test_oversized_autonomous_request_retains_refusal_for_later_dispatch(tmp_path, profile_http):
    from unittest.mock import Mock
    from alx.contracts import AutonomousRequestUnbounded, ReasoningContext
    from alx.core.model_reasoner import ModelReasoner
    from alx.providers.errors import ProviderError

    profile_http[1].status = 403
    model, runner = model_at(tmp_path, sentinel())
    model.verify_identity()
    first = model.setup_token_notice_facts()
    context = ReasoningContext(None, (), (), conversation_id='notice-delivery',
                               runtime=first)
    bounded = ModelReasoner(model, 'laws', 'identity', max_input_tokens=1,
                            spend_authority=Mock(),
                            on_request_dispatched=model.acknowledge_setup_token_notice)
    with pytest.raises(AutonomousRequestUnbounded):
        bounded.decide(context)
    assert len(runner.calls) == 1  # Only login verification, no model send.
    later = model.setup_token_notice_facts()
    assert later == first, 'local input refusal lost the pending diagnostic'
    allowed = ModelReasoner(model, 'laws', 'identity',
                           on_request_dispatched=model.acknowledge_setup_token_notice)
    dispatched = allowed.build_request(ReasoningContext(None, (), (),
        conversation_id='notice-delivery', runtime=later))
    assert json.loads(dispatched.messages[-1].content)['runtime'] == first
    # The stub returns malformed model output: delivery still counts.
    with pytest.raises(ProviderError):
        model.complete(dispatched)
    assert model.setup_token_notice_facts() == {}


def notice_request(model):
    from alx.contracts import ReasoningContext
    from alx.core.model_reasoner import ModelReasoner
    reasoner = ModelReasoner(model, 'laws', 'identity',
                            on_request_dispatched=model.acknowledge_setup_token_notice)
    return reasoner.build_request(ReasoningContext(None, (), (),
        conversation_id='notice-delivery', runtime=model.setup_token_notice_facts()))


@pytest.mark.parametrize('provider', ['openai', 'xai', 'kimi'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('failure', ['success', 'status', 'bad_output', 'connect',
                                   'connect_timeout', 'pool', 'write', 'write_timeout',
                                   'read_timeout', 'read_error', 'protocol', 'preparation'])
def test_http_core_delivers_research_refusal_once(tmp_path, profile_http, provider,
                                                streaming, failure):
    import httpx
    from alx.contracts import ReasoningContext
    from alx.core.model_reasoner import ModelReasoner
    from alx.providers.errors import ProviderError
    from alx.providers.openai import OpenAIReasoningModel
    from alx.providers.xai import XAIReasoningModel

    profile_http[1].status = 403
    research, _ = model_at(tmp_path, sentinel(), ClaudeSubscriptionResearchModel)
    research.verify_identity()
    facts = research.setup_token_notice_facts()
    sent = []

    def respond(incoming):
        if failure == 'connect':
            raise httpx.ConnectError('offline')
        if failure == 'connect_timeout':
            raise httpx.ConnectTimeout('offline')
        if failure == 'pool':
            raise httpx.PoolTimeout('busy')
        if failure == 'write':
            raise httpx.WriteError('incomplete request')
        if failure == 'write_timeout':
            raise httpx.WriteTimeout('incomplete request')
        payload = json.loads(incoming.content)
        messages = payload['input' if provider == 'openai' else 'messages']
        sent.append(json.loads(messages[-1]['content'])['runtime'])
        if failure == 'read_timeout':
            raise httpx.ReadTimeout('no response')
        if failure == 'read_error':
            raise httpx.ReadError('response interrupted')
        if failure == 'protocol':
            raise httpx.RemoteProtocolError('invalid response')
        if failure == 'status':
            return httpx.Response(500)
        content = '{}' if failure == 'success' else 'invalid'
        if streaming:
            event = ({'type': 'response.completed', 'response': {'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}]}}
                if provider == 'openai' else
                {'choices': [{'delta': {'content': content}}]})
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n')
        body = ({'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}]}
                if provider == 'openai' else
                {'choices': [{'message': {'content': content}}]})
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIReasoningModel if provider == 'openai' else XAIReasoningModel
        core = adapter('model', 'synthetic-key', 'https://' + provider + '.test', 30,
                       client=client, streaming=streaming)
        receipts = []

        def acknowledge(runtime):
            receipts.append(runtime)
            research.acknowledge_setup_token_notice(runtime)

        reasoner = ModelReasoner(core, 'laws', 'identity',
            on_request_dispatched=acknowledge)

        def outgoing(runtime):
            return reasoner.build_request(ReasoningContext(None, (), (),
                conversation_id='research-refusal', runtime=runtime))

        first = outgoing(facts)
        assert research.setup_token_notice_facts() == facts
        if failure == 'preparation':
            from dataclasses import replace
            first = replace(first, messages=(object(),))
        if failure == 'success':
            core.complete(first)
        else:
            with pytest.raises((ProviderError, AttributeError)):
                core.complete(first)
        pending = failure in {'connect', 'connect_timeout', 'pool', 'write',
                              'write_timeout', 'preparation'}
        assert receipts == ([] if pending else [facts])
        assert research.setup_token_notice_facts() == (facts if pending else {})
        if pending:
            assert not sent
            return
        assert sent == [facts]
        # A later request carries no warning, even after a failed delivered turn.
        later = outgoing(research.setup_token_notice_facts())
        assert json.loads(later.messages[-1].content)['runtime'] == {}
        # A request assembled without the warning cannot consume a newer one.
        profile = subscription._setup_token_profile
        subscription._setup_token_profile = subscription._SetupTokenProfile(
            profile.token, refusal=profile.refusal)
        newer = research.setup_token_notice_facts()
        if failure == 'success':
            core.complete(later)
        else:
            with pytest.raises(ProviderError):
                core.complete(later)
        assert sent[-1] == {}
        assert receipts == [facts, {}]
        assert research.setup_token_notice_facts() == newer


@pytest.mark.parametrize('failure', ['identity', 'unavailable', 'preparation', 'missing_cli',
                                   'budget', 'timeout', 'bad_output', 'nonzero', 'success'])
def test_refusal_acknowledgement_tracks_provider_send(tmp_path, profile_http, failure):
    import subprocess
    from unittest.mock import Mock
    from alx.core.model_reasoner import ModelReasoner
    from alx.providers.errors import ProviderError

    profile_http[1].status = 403
    model, runner = model_at(tmp_path, sentinel())
    model.verify_identity()
    facts = model.setup_token_notice_facts()
    outgoing = notice_request(model)
    assert facts == model.setup_token_notice_facts()  # Construction is not delivery.
    if failure == 'identity':
        runner.status_stdout = 'invalid'
    elif failure == 'unavailable':
        model.ensure_available = Mock(side_effect=ProviderError('claude_subscription', 'unavailable'))
    elif failure == 'preparation':
        model.command = Mock(side_effect=ValueError('invalid_request'))
    elif failure == 'missing_cli':
        model._runner = Mock(side_effect=FileNotFoundError())
        model._require_identity = Mock()  # Refuse at model spawn, after identity preflight.
    elif failure in {'timeout', 'nonzero'}:
        original = model._runner
        def failing_runner(command, **kwargs):
            if command[1:] == ['auth', 'status', '--json']:
                return original(command, **kwargs)
            assert model.setup_token_notice_facts() == facts
            assert json.loads(kwargs['input'])['runtime'] == facts
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('claude', 30)
            return subprocess.CompletedProcess(command, 1, '', '')
        model._runner = failing_runner
    if failure == 'success':
        from dataclasses import replace
        model.complete(replace(outgoing, output_schema={'type': 'object'}))
    elif failure == 'budget':
        authority = Mock()
        authority.reserve.side_effect = RuntimeError('budget_refused')
        bounded = ModelReasoner(model, 'laws', 'identity', max_input_tokens=10**8,
                                spend_authority=authority)
        with pytest.raises(RuntimeError, match='budget_refused'):
            bounded._spend_for(outgoing)
    else:
        with pytest.raises((ProviderError, SubscriptionIdentityError)):
            model.complete(outgoing)
    assert model.setup_token_notice_facts() == (
        {} if failure in {'timeout', 'bad_output', 'nonzero', 'success'} else facts)


def test_acknowledgement_requires_both_notice_identity_and_text(tmp_path, profile_http):
    profile_http[1].status = 403
    model, _ = model_at(tmp_path, sentinel())
    model.verify_identity()
    facts = model.setup_token_notice_facts()
    for changed in ({'authentication_notice': ''}, {'authentication_notice': 'other'},
                    {'authentication_notice_id': 'other'}):
        model.acknowledge_setup_token_notice({**facts, **changed})
        assert model.setup_token_notice_facts() == facts
    model.acknowledge_setup_token_notice(facts)
    assert model.setup_token_notice_facts() == {}
    newer, _ = model_at(tmp_path, sentinel())
    newer.verify_identity()
    assert newer.setup_token_notice_facts()['authentication_notice_id'] != facts['authentication_notice_id']


def test_old_request_cannot_acknowledge_new_refusal_even_with_identical_wording(tmp_path, profile_http):
    from alx.providers.errors import ProviderError

    profile_http[1].status = 403
    model, runner = model_at(tmp_path, sentinel())
    model.verify_identity()
    outgoing = notice_request(model)
    old = model.setup_token_notice_facts()
    # A second validation produces a new refusal while this request is in flight.
    original = model._runner
    def replace_notice(command, **kwargs):
        if command[1:] != ['auth', 'status', '--json']:
            profile = subscription._setup_token_profile
            subscription._setup_token_profile = subscription._SetupTokenProfile(
                profile.token, refusal=profile.refusal)
        return original(command, **kwargs)
    model._runner = replace_notice
    with pytest.raises(ProviderError):
        model.complete(outgoing)
    newer = model.setup_token_notice_facts()
    assert newer['authentication_notice'] == old['authentication_notice']
    assert newer['authentication_notice_id'] != old['authentication_notice_id']
    # A sent request without the diagnostic must not consume it either.
    model._runner = original
    model.complete(request())
    assert model.setup_token_notice_facts() == newer


@pytest.fixture(autouse=True)
def profile_http(monkeypatch):
    monkeypatch.setattr(subscription, '_setup_token_profile', None)
    monkeypatch.setattr(subscription, '_setup_token_notice_acknowledged', None)
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
    assert model.setup_token_notice_facts() == {}
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
    facts = model.setup_token_notice_facts()
    notice = facts['authentication_notice']
    assert notice.startswith('The Claude setup token was refused because ')
    assert notice.endswith('the normal dedicated Claude login is being used.')
    assert other.setup_token_notice_facts() == facts
    assert model.setup_token_notice_facts() == facts
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
    facts = model.setup_token_notice_facts()
    notice = facts['authentication_notice']
    assert notice and token not in notice + caplog.text, 'credential leaked'
    assert model.setup_token_notice_facts() == facts
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
    assert model.setup_token_notice_facts() == {}
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
        facts = providers.reasoning.setup_token_notice_facts()
        notice = facts['authentication_notice']
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=None, turns=(), capabilities=(),
            conversation_id='token-refusal',
            runtime=facts,
        )))
        assert payload['runtime']['authentication_notice'] == notice
        assert providers.autonomous.setup_token_notice_facts() == facts
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


@pytest.mark.parametrize('redraw', ['plain', 'escape', 'overwrite', 'partial'])
def test_usage_masks_unlabelled_token_before_evidence_is_returned(redraw):
    token = sentinel()
    reader = ClaudeUsageReader('unused', {'CLAUDE_CODE_OAUTH_TOKEN': token})
    fragments = (token[:16], token[16:])
    echoed = {
        'plain': token,
        'escape': fragments[0] + '\x1b[m' + fragments[1],
        'overwrite': fragments[1] + '\r' + fragments[0],
        'partial': fragments[0],
    }[redraw]
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
    for fragment in (token, *fragments):
        assert fragment not in json.dumps(result), 'usage failure leaked credential fragment'
    assert '[masked]' in result['screen_capture']
    assert 'raw_output_tail' not in result
