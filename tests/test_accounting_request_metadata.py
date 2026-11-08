"""Trusted labels are frozen accounting evidence, never provider input or authority."""
from collections.abc import Mapping
import json

import pytest

from prosaic_runtime import ProsaicArtifact, ProsaicRuntime, generic_guards
from prosaic_runtime.accounting import (
    AccountingError, ExecutionContext, MemoryRecorder, ResolvedContext, canonical, fingerprint,
)
from support.conversation_server import ConversationFixture


def fixed_context(**kwargs):
    return ExecutionContext(application_id='app', tenant_id='tenant', billing_account_id='payer',
                            request_id='request', invocation_id='invocation', run_id='run', **kwargs)


def test_legacy_context_and_empty_extension_preserve_public_0916_golden():
    # Captured independently from the installed public0.9.16 wheel, fixed IDs.
    golden = ('{"actor_id":null,"application_id":"app","attribution_source":'
              '{"application_id":"provided","billing_account_id":"provided","tenant_id":"provided"},'
              '"billing_account_id":"payer","invocation_id":"invocation","parent_invocation_id":null,'
              '"project_id":null,"request_id":"request","run_id":"run","step_id":null,"tenant_id":"tenant"}')
    for options in ({}, {'request_metadata': None}, {'request_metadata': {}}):
        context = fixed_context(**options).resolve()
        assert canonical(context.to_dict()) == golden
        assert fingerprint(context.to_dict()) == 'c022cb2f52a608e888a78f72a0dde93af4e200d3d53cb7c8edb527dcd7922aef'
        assert ResolvedContext.from_dict(context.to_dict()) == context


def test_owned_snapshot_decode_copy_and_child_are_immutable():
    supplied = {'run': 'benchmark-42', 'variant': 'cold-cache'}
    context = fixed_context(request_metadata=supplied)
    supplied['variant'] = 'changed'
    with pytest.raises(TypeError):
        context.request_metadata['variant'] = 'changed'
    resolved = context.resolve()
    expected = {'version': 1, 'labels': {'run': 'benchmark-42', 'variant': 'cold-cache'}}
    assert resolved.to_dict()['request_metadata'] == expected
    wire = resolved.to_dict()
    decoded = ResolvedContext.from_dict(wire)
    wire['request_metadata']['labels']['variant'] = 'changed'
    assert decoded == resolved
    assert decoded.to_dict()['request_metadata'] == expected
    child = decoded.child(invocation_id='child', step_id='review')
    assert child.to_dict()['request_metadata'] == expected
    assert child.parent_invocation_id == 'invocation' and child.step_id == 'review'
    with pytest.raises(TypeError):
        resolved.request_metadata['variant'] = 'changed'


def test_metadata_is_explicit_and_not_inherited_from_defaults():
    defaults = ExecutionContext(application_id='app', request_metadata={'variant': 'default'})
    assert 'request_metadata' not in ExecutionContext().resolve(defaults).to_dict()
    assert ExecutionContext(request_metadata={'variant': 'explicit'}).resolve(defaults).request_metadata == {'variant': 'explicit'}


@pytest.mark.parametrize('labels', [
    {'a': 'x' * 256}, {'a' + 'b' * 63: 'value'}, {'a': 'é' * 128},
    {f'key{i}': 'v' for i in range(16)}, {'a.b_c-d': '中文'},
])
def test_valid_boundaries(labels):
    context = ExecutionContext(request_metadata=labels).resolve()
    assert context.request_metadata == labels
    assert ResolvedContext.from_dict(context.to_dict()) == context


RESERVED = ('application_id tenant_id billing_account_id actor_id project_id request_id run_id '
            'invocation_id parent_invocation_id step_id namespace environment provider_id credential_account '
            'authorization api_key api-key headers request_headers prompt messages tools input input_data').split()


@pytest.mark.parametrize('name', RESERVED + [name.upper() for name in RESERVED])
def test_reserved_keys_cannot_masquerade_as_authority_or_request_payload(name):
    with pytest.raises(ValueError) as exc:
        ExecutionContext(request_metadata={name: 'SECRET_SENTINEL'})
    assert 'SECRET_SENTINEL' not in str(exc.value)


@pytest.mark.parametrize('bad', [
    [], 'SECRET_SENTINEL', 1, True, {'a': ''}, {'a': None}, {'a': 1}, {'a': True},
    {'a': {}}, {'a': []}, {'a': 'x' * 257}, {'a': 'é' * 129}, {'a': '\ud800'},
    {'a': 'line\nSECRET_SENTINEL'}, {'a': '\x7f'}, {'a': '\u200b'}, {'': 'v'},
    {'1bad': 'v'}, {'é': 'v'}, {'a b': 'v'}, {'a' * 65: 'v'}, {1: 'v'},
    {f'key{i}': 'v' for i in range(17)}, {f'key{i}': 'x' * 256 for i in range(16)},
    # The canonical serializer escapes Unicode; count those bytes, not raw text.
    {f'key{i}': 'é' * 128 for i in range(6)},
])
def test_invalid_metadata_has_bounded_sanitized_refusal(bad):
    with pytest.raises(ValueError) as exc:
        ExecutionContext(request_metadata=bad)
    assert 'SECRET_SENTINEL' not in str(exc.value)


class DuplicateItems(Mapping):
    def __iter__(self): return iter(['variant'])
    def __len__(self): return 1
    def __getitem__(self, key): return 'first'
    def items(self): return [('variant', 'first'), ('variant', 'second')]


class HookedText(str):
    def __str__(self): raise AssertionError('custom serialization hook called')


@pytest.mark.parametrize('bad', [DuplicateItems(), {'variant': HookedText('secret')},
                               {HookedText('variant'): 'secret'}])
def test_no_duplicate_items_or_custom_text_hooks(bad):
    with pytest.raises(ValueError):
        ExecutionContext(request_metadata=bad)


@pytest.mark.parametrize('extension', [
    None, {}, [], {'version': 1}, {'labels': {'variant': 'v'}},
    {'version': 2, 'labels': {'variant': 'v'}}, {'version': True, 'labels': {'variant': 'v'}},
    {'version': 1, 'labels': {}}, {'version': 1, 'labels': {'variant': 'v'}, 'extra': 'secret'},
    {'version': 1, 'labels': {'tenant_id': 'secret'}},
])
def test_persisted_extension_is_strict(extension):
    wire = fixed_context().resolve().to_dict()
    wire['request_metadata'] = extension
    with pytest.raises(ValueError):
        ResolvedContext.from_dict(wire)


def test_capability_is_public():
    assert 'accounting_request_metadata_v1' in ProsaicRuntime.capabilities


@pytest.fixture(params=['openai-compatible', 'anthropic'])
def wire(request, monkeypatch):
    monkeypatch.setenv('no_proxy', '*')
    with ConversationFixture(request.param) as fx:
        headers = []
        original = fx.http.RequestHandlerClass.do_POST
        def capture(handler):
            headers.append(dict(handler.headers))
            return original(handler)
        monkeypatch.setattr(fx.http.RequestHandlerClass, 'do_POST', capture)
        yield fx, headers


@pytest.mark.parametrize('entry', ['run', 'turn', 'prepared'])
def test_prepare_sees_labels_before_dispatch_without_provider_or_identity_leak(wire, entry):
    fx, headers = wire
    seen = []
    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            assert fx.requests == []
            seen.append(intent['context']['request_metadata'])
            return super().prepare(intent)
    recorder = Recorder(provider_id=fx.provider)
    runner = ProsaicRuntime(fx.runtime.config, accounting=recorder)
    context = fixed_context(request_metadata={'variant': 'UNIQUE_LABEL_NOT_ON_WIRE'})
    agent = fx.agent
    digest = agent.digest
    prepared = runner.prepare_turn(agent, message='hello')
    identity = prepared.context_identity
    fx.queue_text('done')
    if entry == 'run':
        result = runner.run(agent, context=context)
    else:
        result = runner.turn(prepared if entry == 'prepared' else agent, context=context,
                             **({} if entry == 'prepared' else {'message': 'hello'})).result
    assert result.exit_code == 0 and len(fx.requests) == 1
    assert seen == [{'version': 1, 'labels': {'variant': 'UNIQUE_LABEL_NOT_ON_WIRE'}}]
    assert result.metadata['accounting_v1']['context']['request_metadata'] == seen[0]
    assert agent.digest == digest
    assert runner.prepare_turn(agent, message='hello').context_identity == identity
    assert 'UNIQUE_LABEL_NOT_ON_WIRE' not in json.dumps(fx.requests) + json.dumps(headers)


@pytest.mark.parametrize('kind', ['tool', 'repair'])
def test_multiple_requests_retain_snapshot_despite_recorder_and_host_mutation(wire, kind):
    fx, _ = wire
    supplied = {'variant': 'frozen'}
    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            assert len(fx.requests) == len(self.intents)
            assert intent['context']['request_metadata']['labels'] == {'variant': 'frozen'}
            result = super().prepare(intent)
            intent['context']['request_metadata']['labels']['variant'] = 'callback-mutation'
            supplied['variant'] = 'host-mutation'
            return result
    recorder = Recorder(provider_id=fx.provider)
    runner = ProsaicRuntime(fx.runtime.config, accounting=recorder, guards=generic_guards())
    context = fixed_context(request_metadata=supplied)
    if kind == 'tool':
        fx.add_tool('lookup', lambda args: {'found': True})
        runner = ProsaicRuntime(fx.runtime.config, accounting=recorder, custom_tools=fx.tools)
        agent = fx.agent
        fx.queue_calls([{'id': 'one', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
    else:
        agent = ProsaicArtifact('rules', 'subagent', {'tools': []},
            '- ALWAYS answer JSON. <!-- enforce: strict_json(stage=final, on_violation=retry) -->\n')
        fx.queue_text('not JSON'); fx.queue_text('{}')
    result = runner.turn(agent, message='hello', context=context, policy=fx.policy)
    assert result.result.exit_code == 0 and len(fx.requests) == len(recorder.intents) == 2
    assert all(i['context']['request_metadata']['labels'] == {'variant': 'frozen'}
               for i in recorder.intents.values())


@pytest.mark.parametrize('phase', ['prepare', 'observe'])
def test_labelled_accounting_failure_does_not_retry_provider(wire, phase):
    fx, _ = wire
    class Broken(MemoryRecorder):
        def prepare(self, intent):
            if phase == 'prepare': raise AccountingError('private')
            return super().prepare(intent)
        def observe(self, call_id, observation):
            raise AccountingError('private')
    fx.queue_text('done')
    result = ProsaicRuntime(fx.runtime.config, accounting=Broken(provider_id=fx.provider)).run(
        fx.agent, context=fixed_context(request_metadata={'variant': 'frozen'}))
    assert result.metadata['failure_reason'] == 'accounting_failed'
    assert len(fx.requests) == (0 if phase == 'prepare' else 1)


def test_acquisition_and_final_request_keep_labels(server, tmp_path):
    from dataclasses import replace
    from prosaic_runtime import EndpointConfig
    from test_acquisition import policy, read
    from test_runtime import artifact, completion, runtime
    url, requests, responses = server
    (tmp_path / 'input').write_text('synthetic evidence')
    responses.extend([completion('', read()), completion('done')])
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False}), accounting=recorder).run(
        replace(artifact('read'), body='FINAL CONTRACT'), acquisition=artifact('read'),
        cwd=tmp_path, policy=policy(), context=fixed_context(request_metadata={'variant': 'acquisition-label'}))
    assert result.exit_code == 0 and len(requests) == len(recorder.intents) == 2
    assert all(i['context']['request_metadata']['labels'] == {'variant': 'acquisition-label'}
               for i in recorder.intents.values())
    assert 'acquisition-label' not in json.dumps(requests)


def test_label_changes_conflict_under_same_call_id(wire):
    from copy import deepcopy
    fx, _ = wire
    recorder = MemoryRecorder(provider_id=fx.provider)
    fx.queue_text('done')
    assert ProsaicRuntime(fx.runtime.config, accounting=recorder).run(
        fx.agent, context=fixed_context(request_metadata={'variant': 'first'})).exit_code == 0
    original = next(iter(recorder.intents.values()))
    recorder.prepare(deepcopy(original))
    changed = deepcopy(original)
    changed['context']['request_metadata']['labels']['variant'] = 'second'
    with pytest.raises(AccountingError):
        recorder.prepare(changed)
    assert next(iter(recorder.intents.values())) == original


# Imported fixture is shared by the acquisition regression and existing SDK tests.
from test_runtime import server  # noqa: E402,F401
