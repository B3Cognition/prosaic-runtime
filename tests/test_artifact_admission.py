"""Execution admission is pure and shares the invocation metadata contract."""
from copy import deepcopy
from dataclasses import replace

import pytest
import prosaic_runtime as api
from prosaic_runtime import CustomTool, EndpointConfig, ProsaicArtifact, RuntimeConfig
from test_runtime import completion, server
from test_acquisition import policy, read, reply


def artifact(**frontmatter):
    return ProsaicArtifact('approved', 'subagent', frontmatter, 'Return {{args}}.')


def config(*, default='openai', features=None, tool_directories=(), url='http://localhost:9/v1'):
    return RuntimeConfig({
        'openai': EndpointConfig(url, 'approved', features={'streaming': False}),
        'native': EndpointConfig(url, 'approved-native', provider='anthropic',
                                 features=features or {'streaming': False}),
    }, {'fast': 'openai', 'native': 'native'}, default,
        frozenset({'read_file'}), tool_directories=tool_directories)


def validate(value, bindings, **kwargs):
    helper = getattr(api, 'validate_execution_artifact', None)
    assert callable(helper), 'the public pure execution validator is required'
    return helper(value, bindings, **kwargs)


def test_admission_does_not_prepare_configured_cli_directories(tmp_path):
    bindings = config(tool_directories=(str(tmp_path / 'missing-cli-directory'),))
    value = artifact(tools=['host_callback'], extension={'accepted': True})
    original = deepcopy((value, bindings))
    assert validate(value, bindings) is None
    assert (value, bindings) == original
    assert not (tmp_path / 'missing-cli-directory').exists()


@pytest.mark.parametrize('change', [
    {'id': ''}, {'type': 'skill'}, {'frontmatter': []}, {'body': None},
    {'resources': ({'relPath': '../private', 'content': 'data'},)},
    {'resources': None},
])
def test_typed_artifacts_still_require_structural_inspection(change):
    with pytest.raises(ValueError):
        validate(replace(artifact(), **change), config())


@pytest.mark.parametrize('value', [None, 'subagents/approved.md', {'type': 'subagent'}])
def test_pure_admission_requires_an_already_inspected_artifact(value):
    with pytest.raises(ValueError):
        validate(value, config())


@pytest.mark.parametrize('metadata', [
    {'tools': 'full'}, {'tools': {'read_file': True}}, {'tools': [7]},
    {'effort': 'maximum'}, {'effort': ['low']},
    {'model_tier': 'missing'}, {'model_tier': ['fast']},
])
def test_execution_metadata_fails_before_runtime_construction(metadata):
    with pytest.raises(ValueError):
        validate(artifact(**metadata), config())


@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_supported_effort_uses_the_final_selected_provider(effort):
    assert validate(artifact(model_tier='fast', effort=effort), config(default='native')) is None
    with pytest.raises(ValueError, match='Anthropic effort'):
        validate(artifact(model_tier='native', effort=effort), config())


@pytest.mark.parametrize('feature', ['json_mode', 'reasoning_effort', 'effort', 'thinking', 'stream_options'])
def test_known_anthropic_features_are_admitted_against_the_selected_endpoint(feature):
    bindings = config(default='native', features={feature: True})
    with pytest.raises(ValueError, match='Anthropic feature'):
        validate(artifact(), bindings)
    assert validate(artifact(model_tier='fast'), bindings) is None


@pytest.mark.parametrize('disabled', [None, False, 'off', 'false', 'disabled'])
def test_disabled_provider_controls_and_runtime_feature_overrides_remain_valid(disabled):
    features = {'json_mode': disabled, 'web_tools': True, 'transcript': True}
    assert validate(artifact(), config(default='native', features=features)) is None


def test_acquisition_uses_the_final_provider_and_inherits_omitted_metadata():
    final = artifact(model_tier='fast', effort='low', tools=['read_file'])
    acquisition = artifact(effort='low', tools=['read_file'])
    original = deepcopy((final, acquisition))
    assert validate(final, config(default='native'), acquisition=acquisition) is None
    assert (final, acquisition) == original
    assert validate(final, config(default='native'), acquisition=artifact(tools=['read_file'])) is None


@pytest.mark.parametrize('metadata', [
    {'model_tier': 'native'}, {'effort': 'high'}, {'tools': ['write_file']},
])
def test_acquisition_cannot_override_the_final_context_or_tool_requests(metadata):
    final = artifact(model_tier='fast', effort='low', tools=['read_file'])
    with pytest.raises(ValueError, match='acquisition'):
        validate(final, config(), acquisition=artifact(**metadata))


def test_acquisition_structure_is_validated_without_rendering_or_inspection():
    with pytest.raises(ValueError):
        validate(artifact(), config(), acquisition=replace(artifact(), type='skill'))


def test_invalid_or_mutated_selected_configuration_has_a_value_error():
    with pytest.raises(ValueError):
        validate(artifact(), object())
    bindings = config()
    bindings.profiles.pop('openai')
    with pytest.raises(ValueError):
        validate(artifact(), bindings)


def test_native_registry_exports_return_independent_descriptors_without_callbacks():
    def never_called(*args):
        pytest.fail('registry admission must not invoke handlers or authorizers')
    tool = CustomTool('lookup', 'Lookup approved data',
                      {'type': 'object', 'additionalProperties': False}, never_called,
                      'v1', authorize=never_called)
    check = getattr(api, 'validate_custom_tools', None)
    describe = getattr(api, 'custom_descriptors', None)
    assert callable(check) and callable(describe), 'public native registry APIs are required'
    original = {'lookup': tool}
    admitted = check(original)
    original.clear()
    assert admitted == {'lookup': tool}
    descriptors = describe(admitted)
    assert descriptors['lookup']['version'] == 'v1'
    assert descriptors['lookup']['authorization_required'] is True
    descriptors['lookup']['parameters']['type'] = 'array'
    assert describe(admitted)['lookup']['parameters']['type'] == 'object'
    with pytest.raises(ValueError):
        check({'wrong_name': tool})


def test_runtime_rejects_provider_metadata_before_starting_execution():
    events = []
    with pytest.raises(ValueError, match='Anthropic effort'):
        api.ProsaicRuntime(config(default='native')).run(artifact(effort='low'), on_event=events.append)
    assert events == []


def test_local_acquisition_keeps_the_final_route_and_matching_effort(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('verified evidence')
    responses.extend([reply('', read()), completion('done')])
    final = artifact(model_tier='fast', effort='low', tools=['read_file'])
    acquisition = artifact(effort='low', tools=['read_file'])
    result = api.ProsaicRuntime(config(default='native', url=url)).run(
        final, acquisition=acquisition, cwd=tmp_path, policy=policy())
    assert result.exit_code == 0 and result.metadata['profile'] == 'openai'
    assert len(requests) == 2
    assert [request['model'] for request in requests] == ['approved', 'approved']
