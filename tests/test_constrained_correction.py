"""Schema derivation, bounds, wire application and acceptance for constrained tool-request correction."""

import copy
import hashlib
import json

from jsonschema import Draft202012Validator
import pytest

from prosaic_runtime import EndpointConfig
from prosaic_runtime import constrained_correction as cc
from prosaic_runtime.conversation import encode_owned
from prosaic_runtime.provider_types import ProviderTurn
from prosaic_runtime.tool_registry import InvalidToolRequest
from support.constrained_fixtures import FEATURE, FIND_STATION, READ_GAUGE, VALID, WIRES

TOOLS = [
    {'type': 'function', 'function': {'name': name, 'description': f'Synthetic {name}', 'parameters': schema}}
    for name, schema in (('find_station', FIND_STATION), ('read_gauge', READ_GAUGE))
]
FEATURES = {FEATURE: 'json_schema', 'streaming': False}

FIND_WIRE = {
    'type': 'object',
    'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
    'required': ['query', 'limit'],
    'additionalProperties': False,
}
READ_WIRE = {
    'type': 'object',
    'properties': {
        'station': {'type': 'string'},
        'unit': {'type': 'string', 'enum': ['metres', 'feet']},
        'window': {
            'type': 'object',
            'properties': {'start': {'type': 'string'}, 'hours': {'type': 'integer'}},
            'required': ['start', 'hours'],
            'additionalProperties': False,
        },
    },
    'required': ['station', 'unit', 'window'],
    'additionalProperties': False,
}


def branch(name, arguments):
    return {
        'type': 'object',
        'properties': {'tool': {'const': name}, 'arguments': arguments},
        'required': ['tool', 'arguments'],
        'additionalProperties': False,
    }


# An optional tool keeps the envelope's offer to answer without it: one more branch, the answer itself.
ANSWER = {
    'type': 'object',
    'properties': {'answer': {'type': 'string'}},
    'required': ['answer'],
    'additionalProperties': False,
}


def optional(*branches):
    return {'anyOf': [*branches, ANSWER]}


def arguments_of(result):
    """The arguments schema of the first tool branch."""
    schema = result.schema['anyOf'][0] if 'anyOf' in result.schema else result.schema
    return schema['properties']['arguments']


def plan(issue='argumentsSchema', tool='find_station', *, features=FEATURES, batch_size=1, tools=TOOLS, **kw):
    return cc.plan(features, InvalidToolRequest(issue, 0, tool), batch_size=batch_size, tools=tools, **kw)


def sha(schema):
    """The digest Runtime records: over the schema bytes its request encoder sends."""
    return hashlib.sha256(json.dumps(schema, allow_nan=False).encode()).hexdigest()


# Derivation: case (a), a schema-invalid call to an identifiable tool.


def test_arguments_schema_constrains_the_named_tool_and_its_structural_schema():
    result = plan()
    assert result.schema == optional(branch('find_station', FIND_WIRE))
    assert result.record == {
        'version': 1,
        'wire': 'json_schema',
        'issue': 'argumentsSchema',
        'scope': 'arguments',
        'answerBranch': True,
        'schemaSha256': sha(result.schema),
        'droppedKeywords': ['description', 'maxLength', 'maximum', 'minimum'],
    }


def test_nested_objects_enums_and_required_survive_while_assertions_are_dropped():
    result = plan(tool='read_gauge')
    assert result.schema == optional(branch('read_gauge', READ_WIRE))
    assert result.record['droppedKeywords'] == ['format', 'minimum', 'pattern']


def test_property_names_that_spell_keywords_are_data_not_keywords():
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['pattern', 'minimum'],
        'properties': {'pattern': {'type': 'string', 'pattern': 'x'}, 'minimum': {'type': 'integer'}},
    }
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd', 'parameters': schema}}]
    result = plan(tool='probe', tools=tools)
    assert arguments_of(result)['properties'] == {
        'pattern': {'type': 'string'},
        'minimum': {'type': 'integer'},
    }
    assert result.record['droppedKeywords'] == ['pattern']


def test_items_type_lists_any_of_and_nested_additional_properties_are_kept():
    inner = {'type': ['string', 'null'], 'minLength': 1}
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'tags': {'type': 'array', 'items': inner, 'maxItems': 3},
            'pick': {'anyOf': [{'const': 'a'}, {'type': 'integer', 'multipleOf': 2}]},
            'meta': {'type': 'object', 'additionalProperties': {'type': 'string', 'format': 'uri'}},
        },
    }
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd', 'parameters': schema}}]
    arguments = arguments_of(plan(tool='probe', tools=tools))
    assert arguments['properties'] == {
        'tags': {'type': 'array', 'items': {'type': ['string', 'null']}},
        'pick': {'anyOf': [{'const': 'a'}, {'type': 'integer'}]},
        'meta': {'type': 'object', 'additionalProperties': {'type': 'string'}},
    }


def test_derivation_never_mutates_the_offered_tools():
    tools = copy.deepcopy(TOOLS)
    plan(tools=tools)
    plan('toolNotGranted', None, tools=tools)
    assert tools == TOOLS


# Derivation: case (b), an ungranted tool name.


def test_tool_not_granted_constrains_name_and_arguments_per_granted_tool():
    result = plan('toolNotGranted', None)
    assert result.schema == optional(branch('find_station', FIND_WIRE), branch('read_gauge', READ_WIRE))
    assert result.record['scope'] == 'arguments' and result.record['issue'] == 'toolNotGranted'
    assert result.record['droppedKeywords'] == ['description', 'format', 'maxLength', 'maximum', 'minimum', 'pattern']


def test_a_required_first_tool_narrows_the_ungranted_resend_to_that_tool():
    result = plan('toolNotGranted', None, required_tool='read_gauge')
    assert result.schema == branch('read_gauge', READ_WIRE)  # A required tool: no answer branch.
    assert result.record['answerBranch'] is False


def big_enum_tool(name, count):
    values = [f'{name}-value-{index:04d}-padding' for index in range(count)]
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['code'],
        'properties': {'code': {'type': 'string', 'enum': values}},
    }
    return {'type': 'function', 'function': {'name': name, 'description': 'd', 'parameters': schema}}


def test_ungranted_resend_constrains_the_name_only_when_branches_exceed_the_bounds():
    tools = [big_enum_tool('alpha', 320), big_enum_tool('beta', 320)]
    for tool in tools:  # Each schema fits alone; together they do not.
        assert len(encode_owned(tool['function']['parameters'])) < cc.MAX_SCHEMA_BYTES
    result = plan('toolNotGranted', None, tools=tools)
    assert result.schema == optional(
        {
            'type': 'object',
            'additionalProperties': False,
            'required': ['tool', 'arguments'],
            'properties': {'tool': {'enum': ['alpha', 'beta']}, 'arguments': {'type': 'object'}},
        }
    )
    assert result.record['scope'] == 'name' and result.record['droppedKeywords'] == []
    assert result.record['schemaSha256'] == sha(result.schema)


# Bounds and fallbacks: never refuse the turn, record why.


def test_an_oversize_schema_falls_back_to_ordinary_correction():
    tools = [big_enum_tool('alpha', 700)]
    result = plan(tool='alpha', tools=tools)
    assert result.schema is None
    assert result.record == {'version': 1, 'wire': 'json_schema', 'issue': 'argumentsSchema', 'fallback': 'oversize'}


def test_a_schema_too_deep_once_wrapped_falls_back():
    leaf = {'type': 'string'}
    for _ in range(31):  # 63 levels: a valid tool schema, but over the depth bound of 64 once wrapped.
        leaf = {'type': 'object', 'additionalProperties': False, 'properties': {'n': leaf}}
    tools = [{'type': 'function', 'function': {'name': 'deep', 'description': 'd', 'parameters': leaf}}]
    assert plan(tool='deep', tools=tools).record['fallback'] == 'oversize'


@pytest.mark.parametrize(
    'issue, tool',
    [
        ('argumentsMalformed', 'find_station'),
        ('argumentsTooLarge', 'find_station'),
        ('callShape', None),
        ('argumentsSchema', None),
        ('argumentsSchema', 'unknown_tool'),
    ],
)
def test_other_issues_and_unidentifiable_tools_keep_ordinary_correction(issue, tool):
    result = plan(issue, tool)
    assert result.schema is None and result.record['fallback'] == 'issue'


def test_a_multi_call_batch_keeps_ordinary_correction():
    assert plan(batch_size=2).record['fallback'] == 'batch'


def test_no_plan_without_the_opt_in():
    assert plan(features={'streaming': False}) is None


def test_the_digest_is_over_the_schema_bytes_runtime_sends():
    # The request encoder is json.dumps(payload, allow_nan=False): a subtree encodes to these exact bytes.
    assert (
        plan().record['schemaSha256']
        == plan().record['schemaSha256']
        == hashlib.sha256(json.dumps(plan().schema, allow_nan=False).encode()).hexdigest()
    )


# Malformed or unusual tool schema shapes: never a crash; a correct derivation or a recorded fallback.
# CustomTool, MCP and builtin schemas are Draft 2020-12 checked, so these reach plan() only from a host's own
# registry; plan() takes the offered tools as given and must not trust them.


def probe(parameters, issue='argumentsSchema'):
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd', 'parameters': parameters}}]
    return plan(issue, 'probe' if issue == 'argumentsSchema' else None, tools=tools)


def deep_items(levels):
    schema = {'type': 'string'}
    for _ in range(levels):
        schema = {'items': schema}
    return {'type': 'object', 'properties': {'a': schema}}


@pytest.mark.parametrize(
    'parameters',
    [
        {'type': 'object', 'properties': {'pair': {'type': 'array', 'items': [{'type': 'string', 'pattern': 'x'}]}}},
        {'type': 'object', 'properties': [1, 2]},
        {'type': 'object', 'properties': {}, 'required': 'x'},
        {'type': 'object', 'required': [1]},
        {'type': 'object', 'properties': {'a': {'anyOf': 'xy'}}},
        {'type': 'object', 'properties': {'a': {'anyOf': []}}},
        {'type': 'object', 'properties': {'a': {'enum': 'xy'}}},
        {'type': 'object', 'properties': {'a': {'type': 7}}},
        {'type': 'object', 'properties': {'a': {'items': 'x'}}},
        {'type': 'object', 'additionalProperties': 'no'},
        {'type': 'object', 'properties': {'a': 3}},
        None,
        False,
        True,
        [],
        deep_items(1200),
    ],
    ids=[
        'items-list',
        'properties-list',
        'required-string',
        'required-non-string',
        'anyOf-string',
        'anyOf-empty',
        'enum-string',
        'type-number',
        'items-string',
        'additionalProperties-string',
        'property-number',
        'parameters-null',
        'parameters-false',
        'parameters-true',
        'parameters-list',
        'deeper-than-recursion',
    ],
)
def test_unusable_shapes_fall_back_with_a_recorded_reason(parameters):
    result = probe(parameters)
    assert result.schema is None
    assert result.record['fallback'] in ('unsupported', 'oversize')


def test_a_deep_schema_is_an_oversize_fallback_not_a_crash():
    assert probe(deep_items(1200)).record['fallback'] == 'oversize'


def test_a_tuple_form_items_list_is_unsupported_so_nothing_inside_it_reaches_the_wire_unrecorded():
    parameters = {'type': 'object', 'properties': {'pair': {'type': 'array', 'items': [{'type': 'string'}]}}}
    assert probe(parameters).record == {
        'version': 1,
        'wire': 'json_schema',
        'issue': 'argumentsSchema',
        'fallback': 'unsupported',
    }


@pytest.mark.parametrize(
    'parameters, arguments',
    [
        ({}, {}),
        (
            {'type': 'object', 'properties': {}, 'additionalProperties': False},
            {'type': 'object', 'properties': {}, 'additionalProperties': False},
        ),
        (
            {'type': 'object', 'properties': {'a': True, 'b': False}},
            {'type': 'object', 'properties': {'a': True, 'b': False}},
        ),
        (
            {'type': 'object', 'properties': {'a': {'type': 'array', 'items': False}}},
            {'type': 'object', 'properties': {'a': {'type': 'array', 'items': False}}},
        ),
    ],
    ids=['parameters-empty', 'properties-empty', 'boolean-subschemas', 'items-false'],
)
def test_valid_unusual_shapes_derive_correctly(parameters, arguments):
    result = probe(parameters)
    assert arguments_of(result) == arguments
    Draft202012Validator.check_schema(result.schema)


def test_refs_and_defs_are_dropped_and_recorded_never_left_dangling():
    parameters = {'type': 'object', '$defs': {'s': {'type': 'string'}}, 'properties': {'a': {'$ref': '#/$defs/s'}}}
    result = probe(parameters)
    assert arguments_of(result) == {'type': 'object', 'properties': {'a': {}}}
    assert '$ref' not in json.dumps(result.schema) and result.record['droppedKeywords'] == ['$defs', '$ref']


def test_an_unusable_granted_tool_leaves_the_ungranted_resend_constrained_by_name():
    tools = [*TOOLS, {'type': 'function', 'function': {'name': 'odd', 'description': 'd', 'parameters': None}}]
    result = plan('toolNotGranted', None, tools=tools)
    assert result.record['scope'] == 'name'
    assert result.schema['anyOf'][0]['properties']['tool'] == {'enum': ['find_station', 'read_gauge', 'odd']}
    assert result.schema['anyOf'][1] == ANSWER


def test_dropped_keyword_names_are_bounded_in_the_evidence():
    properties = {f'p{index}': {'type': 'string', f'x-annotation-{index:03d}-{"n" * 40}': True} for index in range(200)}
    result = probe({'type': 'object', 'properties': properties})
    dropped = result.record['droppedKeywords']
    assert len(dropped) <= cc.MAX_DROPPED_NAMES and len(json.dumps(dropped)) <= cc.MAX_DROPPED_BYTES
    assert result.record['droppedKeywordsOmitted'] == 200 - len(dropped)
    assert dropped == sorted(dropped)


def test_no_omitted_count_when_every_dropped_name_fits():
    assert 'droppedKeywordsOmitted' not in plan().record


# Wire application.


def base_payload():
    return {
        'model': 'fixture-model',
        'messages': [],
        'tools': copy.deepcopy(TOOLS),
        'tool_choice': 'auto',
        'response_format': {'type': 'json_object'},
    }


def test_json_schema_wire_sends_strict_response_format_and_no_tool_choice():
    payload, result = base_payload(), plan()
    cc.apply(payload, result, FEATURES)
    assert payload['response_format'] == {
        'type': 'json_schema',
        'json_schema': {'name': 'tool_request', 'schema': result.schema, 'strict': True},
    }
    assert payload['tool_choice'] == 'none' and payload['tools'] == TOOLS and 'structured_outputs' not in payload


def test_omlx_grammar_wire_sends_structured_outputs_json():
    features = {FEATURE: 'omlx_grammar', 'streaming': False}
    payload, result = base_payload(), plan(features=features)
    cc.apply(payload, result, features)
    assert payload['structured_outputs'] == {'json': result.schema}
    assert 'response_format' not in payload and payload['tool_choice'] == 'none'


# Acceptance: only content that fits the constraint becomes one host-made call.


def turn(content, *, finish_reason='stop', tool_calls=()):
    return ProviderTurn(
        text=content,
        finish_reason=finish_reason,
        token_usage=5,
        token_usage_details={},
        raw_response_metadata={},
        reasoning_content_observed=False,
        tool_calls=list(tool_calls),
        streamed=False,
        http_status=200,
        raw_response_headers={},
    )


def test_conforming_content_becomes_one_host_made_call():
    resend = {'tool': 'find_station', 'arguments': VALID['find_station']}
    candidate = turn(json.dumps(resend))
    assert cc.accept(candidate, plan(), [])
    assert candidate.text == '' and candidate.finish_reason == 'tool_calls'
    assert candidate.tool_calls == [
        {
            'id': 'correction-1',
            'type': 'function',
            'function': {'name': 'find_station', 'arguments': json.dumps(VALID['find_station'], ensure_ascii=False)},
        }
    ]


def test_the_host_made_call_id_is_unique_within_the_turn():
    history = [{'role': 'assistant', 'tool_calls': [{'id': 'correction-1'}, {'id': 'correction-2'}]}]
    candidate = turn(json.dumps({'tool': 'find_station', 'arguments': VALID['find_station']}))
    assert cc.accept(candidate, plan(), history) and candidate.tool_calls[0]['id'] == 'correction-3'


@pytest.mark.parametrize(
    'content, finish_reason, tool_calls',
    [
        ('Station ST-104.', 'stop', ()),  # Text: the provider ignored the constraint.
        ('{"tool":"find_station","arguments":{"query":"North Pier"}}', 'stop', ()),  # Misses a required argument.
        ('{"tool":"read_gauge","arguments":{"query":"North Pier","limit":1}}', 'stop', ()),  # Another tool.
        ('{"tool":"find_station","tool":"find_station","arguments":{"query":"a","limit":1}}', 'stop', ()),
        ('{"tool":"find_station","arguments":{"query":"a","limit":NaN}}', 'stop', ()),
        ('{"tool":"find_station","arguments":{"query":"a","limit":1}}', 'length', ()),
        (
            '',
            'tool_calls',
            (
                {
                    'id': 'x',
                    'type': 'function',
                    'function': {'name': 'find_station', 'arguments': '{"query":"a","limit":1}'},
                },
            ),
        ),  # A native call, even a valid one.
        ('[]', 'stop', ()),
    ],
)
def test_anything_else_is_not_accepted(content, finish_reason, tool_calls):
    candidate = turn(content, finish_reason=finish_reason, tool_calls=tool_calls)
    assert not cc.accept(candidate, plan(), [])


# Profile validation.


def endpoint(**features):
    return EndpointConfig('http://127.0.0.1:9/v1', 'fixture-model', features=features)


@pytest.mark.parametrize('wire', WIRES)
def test_a_nonstreaming_openai_compatible_profile_accepts_either_wire(wire):
    assert endpoint(**{FEATURE: wire, 'streaming': False}).features[FEATURE] == wire


@pytest.mark.parametrize(
    'features',
    [
        {FEATURE: True, 'streaming': False},
        {FEATURE: 'grammar', 'streaming': False},
        {FEATURE: None, 'streaming': False},
        {FEATURE: ''},
        {FEATURE: 'json_schema'},
        {FEATURE: 'json_schema', 'streaming': True},
        {FEATURE: 'json_schema', 'streaming': False, 'omlx_tool_call_envelope': True, 'finalize_after_tool_rounds': 1},
    ],
)
def test_invalid_values_and_combinations_are_refused(features):
    with pytest.raises(ValueError, match=FEATURE):
        endpoint(**features)


def test_the_nemotron_flag_and_the_general_opt_in_are_exclusive():
    from prosaic_runtime.config import _OMLX_NEMOTRON_MODEL  # The only profile the Nemotron flag accepts.

    with pytest.raises(ValueError, match=FEATURE):
        EndpointConfig(
            'http://127.0.0.1:9/v1',
            _OMLX_NEMOTRON_MODEL,
            features={FEATURE: 'omlx_grammar', 'streaming': False, 'omlx_nemotron_tool_correction': True},
        )


def test_the_anthropic_wire_is_refused():
    with pytest.raises(ValueError, match=FEATURE):
        EndpointConfig(
            'http://127.0.0.1:9/v1',
            'fixture-model',
            provider='anthropic',
            features={FEATURE: 'json_schema', 'streaming': False},
        )
