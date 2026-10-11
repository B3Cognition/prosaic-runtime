"""Pure conversation admission and the shared initial provider projection."""
from dataclasses import asdict, fields, replace
import hashlib
import json
from pathlib import Path

from .admission import validate_execution_artifact
from .artifacts import ProsaicArtifact
from .anthropic import AnthropicBackend, encode_messages
from .openai_compatible import OpenAICompatibleBackend, _OpenAIToolRegistry, _feature_enabled
from .policy import RunPolicy, requested_tools, BUILTIN_TOOLS, READ_TOOLS, WRITE_TOOLS
from .conversation import (PreparedTurn, TurnLimits, TurnAdmissionError, encode_owned,
                           validate_conversation_history)


def policy_document(policy):
    return {**{f.name: getattr(policy, f.name) for f in fields(policy)},
            'allowed_tools': sorted(policy.allowed_tools),
            'read_roots': list(policy.read_roots), 'write_paths': list(policy.write_paths),
            'forbidden_roots': list(policy.forbidden_roots)}


def policy_from_document(document):
    return RunPolicy(**{**document, 'allowed_tools': frozenset(document['allowed_tools']),
                        'read_roots': tuple(document['read_roots']),
                        'write_paths': tuple(document['write_paths']),
                        'forbidden_roots': tuple(document['forbidden_roots'])})


def turn_endpoint(runtime, artifact, policy):
    tier = artifact.frontmatter.get('model_tier')
    profile = runtime.config.routes[tier] if tier is not None else runtime.config.default_profile
    endpoint = runtime.config.profiles[profile]
    tools = requested_tools(artifact.frontmatter.get('tools'))
    endpoint = replace(endpoint, features={**endpoint.features, 'tool_calls': bool(tools),
        'max_tool_rounds': policy.max_tool_rounds, 'web_tools': False, 'transcript': False})
    metadata = {'model': endpoint.model, 'allowed_tools': sorted(tools),
                'tool_read_roots': list(policy.read_roots), 'tool_write_paths': list(policy.write_paths),
                'tool_forbidden_roots': list(policy.forbidden_roots)}
    if artifact.frontmatter.get('effort') is not None:
        metadata['effort'] = artifact.frontmatter['effort']
    if policy.initial_tool is not None:
        metadata['initial_tool'] = policy.initial_tool
    return profile, endpoint, metadata


def encode_history_messages(history, *, provider):
    """Map owned neutral data to private adapter input without executing calls."""
    messages, index = [], 0
    native_ids = {}
    for message in history:
        role, blocks = message['role'], message['content']
        if role == 'user':
            text = '\n'.join(b['text'] for b in blocks)
            if message['turnStatus'] == 'failed':
                # Explicit unprivileged envelope preserves status across merged
                # native consecutive user messages; no fabricated answer.
                text = json.dumps({'text': text, 'turnStatus': 'failed',
                                   'failureCode': message['failureCode']}, ensure_ascii=False)
            messages.append({'role': 'user', 'content': text})
        elif role == 'assistant':
            text, calls, native = [], [], []
            for block in blocks:
                if block['type'] == 'text':
                    text.append(block['text'])
                    native.append({'type': 'text', 'text': block['text']})
                else:
                    index += 1
                    private_id = f'history_{index}'
                    native_ids[block['callId']] = private_id
                    calls.append({'id': private_id, 'type': 'function', 'function': {
                        'name': block['name'], 'arguments': json.dumps(block['arguments'], allow_nan=False)}})
                    native.append({'type': 'tool_use', 'id': private_id, 'name': block['name'],
                                   'input': block['arguments']})
            encoded = {'role': 'assistant', 'content': '\n'.join(text)}
            if calls:
                encoded['tool_calls'] = calls
            if provider == 'anthropic':
                encoded['provider_content'] = native
            messages.append(encoded)
        else:
            for block in blocks:
                messages.append({'role': 'tool', 'tool_call_id': native_ids[block['callId']],
                    'content': json.dumps(block['outcome'], allow_nan=False)})
    return messages


def initial_projection(artifact, history, message, input_data, endpoint):
    instructions = artifact.render()
    current = message if input_data is None else json.dumps(
        {'message': message, 'inputData': input_data}, ensure_ascii=False, allow_nan=False)
    return [{'role': 'system', 'content': instructions},
            *encode_history_messages(history, provider=endpoint.provider),
            {'role': 'user', 'content': current}]


def tool_schemas(runtime, names):
    builtin = [{'type': 'function', 'function': {'name': t.name, 'description': t.description,
               'parameters': t.parameters}} for t in _OpenAIToolRegistry._tools() if t.name in names]
    custom = [{'type': 'function', 'function': {'name': name, 'description': d['description'],
               'parameters': d['parameters']}} for name, d in sorted(runtime.tool_descriptors.items())
              if name in names]
    return builtin + custom


def initial_payload(runtime, artifact, policy, history, message, input_data):
    profile, endpoint, metadata = turn_endpoint(runtime, artifact, policy)
    backend = AnthropicBackend(endpoint) if endpoint.provider == 'anthropic' else OpenAICompatibleBackend(endpoint)
    messages = initial_projection(artifact, history, message, input_data, endpoint)
    streaming = _feature_enabled(endpoint.features, 'streaming', default=True)
    payload = backend._chat_payload(messages, metadata, streaming=streaming,
                                    tools=tool_schemas(runtime, metadata['allowed_tools']))
    if policy.initial_tool is not None and payload.get('tools'):
        payload['tool_choice'] = {'type': 'function', 'function': {'name': policy.initial_tool}}
    native = encode_messages(payload) if endpoint.provider == 'anthropic' else payload
    return profile, endpoint, metadata, messages, native


def prepare_turn(runtime, artifact, *, message, history=(), input_data=None, policy=None,
                 limits=None, workspace=None):
    limits = limits if limits is not None else TurnLimits()
    if type(limits) is not TurnLimits:
        raise TurnAdmissionError('invalidPolicy')
    owned_history = validate_conversation_history(history, limits=limits)
    try:
        if type(artifact) is not ProsaicArtifact:
            raise ValueError('already inspected artifact required')
        raw_agent = {'id': artifact.id, 'type': artifact.type, 'frontmatter': artifact.frontmatter,
                     'body': artifact.body, 'resources': list(artifact.resources)}
        raw_agent = json.loads(encode_owned(raw_agent, max_bytes=limits.max_history_bytes,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes))
        owned_agent = ProsaicArtifact.from_inspection(raw_agent)
        validate_execution_artifact(owned_agent, runtime.config)
        if '{{args}}' in owned_agent.body or any('{{args}}' in r['content'] for r in owned_agent.resources):
            raise ValueError('argument interpolation is not conversational')
        requested = requested_tools(owned_agent.frontmatter.get('tools'))
    except (TypeError, ValueError, AttributeError, UnicodeError) as exc:
        raise TurnAdmissionError('invalidArtifact') from exc
    try:
        policy = policy if policy is not None else RunPolicy(max_input_bytes=limits.max_input_bytes,
            max_provider_requests=8, max_tool_calls=16)
        if type(policy) is not RunPolicy or policy.max_provider_requests is None or policy.max_tool_calls is None:
            raise ValueError('finite caps required')
        if type(policy.allowed_tools) is not frozenset or any(type(n) is not str for n in policy.allowed_tools):
            raise ValueError('invalid tool grant')
        for root_field in ('read_roots', 'write_paths', 'forbidden_roots'):
            roots = getattr(policy, root_field)
            if type(roots) is not tuple or any(type(p) is not str or not p or '\x00' in p or len(p.encode('utf-8')) > 4096 for p in roots):
                raise ValueError('invalid path scope')
        policy = policy_from_document(json.loads(encode_owned(policy_document(policy))))
        names = requested & runtime.config.allowed_tools & policy.allowed_tools
        if names != requested or requested - BUILTIN_TOOLS - runtime.tool_descriptors.keys():
            raise ValueError('tools not granted or registered')
        if requested & (BUILTIN_TOOLS | runtime.cli_tool_names) and workspace is None:
            raise ValueError('workspace required')
        if workspace is not None and (not isinstance(workspace, (str, Path)) or not Path(workspace).is_absolute()):
            raise ValueError('absolute private workspace required')
        if requested & READ_TOOLS and not policy.read_roots:
            raise ValueError('read scope required')
        if requested & WRITE_TOOLS and not policy.write_paths:
            raise ValueError('write scope required')
        if policy.initial_tool is not None and policy.initial_tool not in names:
            raise ValueError('initial tool not granted')
        policy = replace(policy, max_input_bytes=min(policy.max_input_bytes, limits.max_input_bytes))
    except (TypeError, ValueError, AttributeError, UnicodeError) as exc:
        raise TurnAdmissionError('invalidPolicy') from exc
    try:
        if type(message) is not str or not message.strip():
            raise ValueError('user text required')
        if len(message.encode('utf-8')) > limits.max_user_bytes:
            raise TurnAdmissionError('inputLimit', 'maxUserBytes')
        if input_data is not None and type(input_data) is not dict:
            raise ValueError('input data must be a mapping')
        data = json.loads(encode_owned(input_data, max_bytes=limits.max_input_bytes,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes))
    except TurnAdmissionError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TurnAdmissionError('inputLimit', 'inputData') from exc
    try:
        profile, endpoint, metadata, messages, native = initial_payload(
            runtime, owned_agent, policy, owned_history, message, data)
        # This is the exact byte encoding used by both existing transports.
        input_bytes = len(json.dumps(native, allow_nan=False).encode('utf-8'))
        if input_bytes > policy.max_input_bytes:
            raise TurnAdmissionError('contextLimit', 'maxInputBytes')
        # Validate before copying; dataclasses.asdict recursively expands
        # aliases/cycles without the bounded JSON traversal.
        endpoint_identity = {f.name: getattr(endpoint, f.name) for f in fields(endpoint)}
        endpoint_identity.pop('api_key_env', None)
        endpoint_identity.pop('api_key_file', None)
        material = {'agent': raw_agent, 'history': owned_history, 'message': message, 'inputData': data,
                    'policy': policy_document(policy), 'limits': asdict(limits), 'workspace': str(workspace) if workspace is not None else None,
                    'profile': profile, 'endpoint': endpoint_identity,
                    'tools': {n: runtime.tool_descriptors[n] for n in sorted(names - BUILTIN_TOOLS)},
                    'messages': messages}
        encoded = encode_owned(material, max_bytes=limits.max_history_bytes + limits.max_input_bytes * 2,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)
        identity_material = {k: v for k, v in material.items() if k != 'workspace'}
        identity = hashlib.sha256(encode_owned(identity_material,
            max_bytes=limits.max_history_bytes + limits.max_input_bytes * 2,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)).hexdigest()
        return PreparedTurn._create(encoded, identity, input_bytes)
    except TurnAdmissionError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise TurnAdmissionError('invalidPolicy') from exc
