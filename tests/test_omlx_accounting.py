"""oMLX usage extensions must not hide ambiguous or additional paid usage."""
from copy import deepcopy
import json

import pytest

from prosaic_runtime import EndpointConfig
from prosaic_runtime.accounting import MemoryRecorder, RateCard, normalize_usage
from test_runtime import artifact, runtime, server


USAGE = {
    'prompt_tokens': 2430, 'completion_tokens': 570, 'total_tokens': 3000,
    'input_tokens': 2430, 'output_tokens': 570,
    'prompt_tokens_details': {'cached_tokens': 2425},
    'completion_tokens_details': {'reasoning_tokens': 0},
    'time_to_first_token': .7, 'total_time': 25.2,
    'prompt_eval_duration': .7, 'generation_duration': 24.5,
    'prompt_tokens_per_second': 7.19, 'generation_tokens_per_second': 23.26,
}
METRICS = (
    'time_to_first_token', 'total_time', 'prompt_eval_duration',
    'generation_duration', 'prompt_tokens_per_second', 'generation_tokens_per_second',
)


def test_matching_token_aliases_and_performance_fields_preserve_text_quantities():
    raw = deepcopy(USAGE)
    assert normalize_usage(raw) == {
        'input_tokens': 2430, 'output_tokens': 570, 'total_tokens': 3000,
        'cached_input_tokens': 2425, 'reasoning_output_tokens': 0, 'status': 'reported',
    }
    assert raw == USAGE


@pytest.mark.parametrize('alias,canonical', [
    ('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
])
@pytest.mark.parametrize('bad', [True, None, -1, 2**63, 1.0, '2430', 2431])
def test_invalid_or_conflicting_token_alias_is_untrusted(alias, canonical, bad):
    raw = deepcopy(USAGE)
    raw[alias] = bad
    assert normalize_usage(raw)['status'] == 'untrusted'


@pytest.mark.parametrize('alias,canonical', [
    ('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
])
def test_alias_without_canonical_quantity_is_untrusted(alias, canonical):
    raw = deepcopy(USAGE)
    del raw[canonical]
    assert normalize_usage(raw)['status'] == 'untrusted'


@pytest.mark.parametrize('metric', METRICS)
@pytest.mark.parametrize('bad', [True, None, 'fast', -1, float('nan'), float('inf'), 10**100])
def test_invalid_performance_metadata_is_untrusted(metric, bad):
    raw = deepcopy(USAGE)
    raw[metric] = bad
    assert normalize_usage(raw)['status'] == 'untrusted'


@pytest.mark.parametrize('metric', METRICS)
@pytest.mark.parametrize('value', [0, .25, 2**63-1])
def test_bounded_nonnegative_performance_metadata_is_nonchargeable(metric, value):
    raw = deepcopy(USAGE)
    raw[metric] = value
    assert normalize_usage(raw)['status'] == 'reported'
    card = RateCard('fixture-v1', 'test', '2', '8', '0.5')
    assert card.assess({'response_model': 'test', 'usage': normalize_usage(raw)})['amount'] == '0.005782500000000000'


@pytest.mark.parametrize('field', ['audio_tokens', 'image_tokens', 'custom_meter'])
def test_extra_unrecognized_or_paid_resources_remain_unpriced(field):
    raw = deepcopy(USAGE)
    raw[field] = 1
    usage = normalize_usage(raw)
    assert usage['status'] == 'unsupported'
    assert RateCard('fixture-v1', 'test', '2', '8').assess({'response_model': 'test', 'usage': usage})['amount'] is None


@pytest.mark.parametrize('change', [
    {'prompt_tokens': True}, {'total_tokens': 3001}, {'completion_tokens': -1},
    {'prompt_tokens_details': {'cached_tokens': 2431}},
    {'completion_tokens_details': {'reasoning_tokens': 571}},
])
def test_aliases_do_not_relax_existing_quantity_checks(change):
    raw = deepcopy(USAGE)
    raw.update(change)
    assert normalize_usage(raw)['status'] == 'untrusted'


def test_aliases_do_not_relax_nested_paid_resource_checks():
    raw = deepcopy(USAGE)
    raw['completion_tokens_details']['audio_tokens'] = 1
    assert normalize_usage(raw)['status'] == 'unsupported'


@pytest.mark.parametrize('streaming', [False, True])
def test_real_runtime_accounting_capture_accepts_omlx_usage(server, streaming):
    url, requests, responses = server
    response = {'id': 'synthetic-omlx', 'model': 'test', 'usage': deepcopy(USAGE),
                'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}]}
    if streaming:
        response['choices'] = [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]
        responses.append(('text/event-stream', ('data: '+json.dumps(response)+'\n\ndata: [DONE]\n\n').encode()))
    else:
        responses.append(('application/json', json.dumps(response).encode()))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'test', features={'streaming': streaming}), accounting=recorder).run(artifact())
    assert result.exit_code == 0 and result.stdout == 'done' and result.token_usage == 3000
    assert len(requests) == len(recorder.intents) == len(recorder.observations) == 1
    observation = next(iter(recorder.observations.values()))
    assert observation['provider_request_id'] == 'synthetic-omlx'
    assert observation['usage']['status'] == 'reported'
    assert observation['usage']['total_tokens'] == 3000


def test_conflicting_omlx_alias_keeps_bounded_usage_unknown():
    from test_transport_diagnostics import BODY, run_wire
    body = BODY.replace(b'"prompt_tokens":5', b'"prompt_tokens":5,"input_tokens":6')
    assert body != BODY
    result, _, recorder, requests = run_wire(body=body, max_reported_tokens=10)
    assert len(requests) == 1
    assert next(iter(recorder.observations.values()))['usage']['status'] == 'untrusted'
    assert result.exit_code != 0 and result.token_usage is None
