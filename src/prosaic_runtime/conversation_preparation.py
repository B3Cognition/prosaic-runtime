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
from .rules import RuleAdmissionError, turn_rules
from .conversation import (PreparedTurn, TurnLimits, TurnAdmissionError, encode_owned,
                           validate_conversation_history)
from .tool_event_contract import current_bindings, tool_contract_preparation


def policy_document(policy):
    if policy.allowed_terminal_tools:
        raise ValueError('terminal tools are unsupported in conversation turns')
    return {**{f.name: getattr(policy, f.name) for f in fields(policy)
               if f.name not in {'allowed_terminal_tools', 'parallel_tools'}},
            **({'parallel_tools': policy.parallel_tools.to_dict()} if policy.parallel_tools is not None else {}),
            'allowed_tools': sorted(policy.allowed_tools),
            'read_roots': list(policy.read_roots), 'write_paths': list(policy.write_paths),
            'forbidden_roots': list(policy.forbidden_roots)}


def policy_from_document(document):
    document = dict(document)
    if 'parallel_tools' in document:
        from .tool_parallel_contracts import ParallelToolPolicyV1
        document['parallel_tools'] = ParallelToolPolicyV1.from_dict(document['parallel_tools'])
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


def current_user_text(message,input_data):
    return message if input_data is None else json.dumps(
        {'message': message, 'inputData': input_data}, ensure_ascii=False, allow_nan=False)


def initial_projection(artifact, history, message, input_data, endpoint):
    instructions = artifact.render()
    bindings = current_bindings()
    if bindings is not None and bindings.citations is not None:
        from .citations import CITATION_INSTRUCTIONS
        instructions = CITATION_INSTRUCTIONS + '\n\n' + instructions
    current = current_user_text(message,input_data)
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


def prepared_endpoint(runtime, artifact, policy, context_policy=None):
    profile, endpoint, metadata = turn_endpoint(runtime, artifact, policy)
    if context_policy is not None:
        if any(name in endpoint.features for name in ('max_completion_tokens', 'max_output_tokens')):
            raise TurnAdmissionError('unsupportedCapability')
        endpoint = replace(endpoint, max_tokens=min(endpoint.max_tokens, context_policy.reserved_output_tokens)
            if endpoint.max_tokens is not None else context_policy.reserved_output_tokens)
    return profile, endpoint, metadata


def initial_payload(runtime, artifact, policy, history, message, input_data, *, context_policy=None, canonical_messages=False):
    profile,endpoint,metadata=prepared_endpoint(runtime,artifact,policy,context_policy)
    backend = AnthropicBackend(endpoint) if endpoint.provider == 'anthropic' else OpenAICompatibleBackend(endpoint)
    messages = initial_projection(artifact, history, message, input_data, endpoint)
    if canonical_messages:
        # Execution uses the owned prepared messages. Match their key order
        # before hashing/counting the native request for the new v3 profile.
        messages=json.loads(encode_owned(messages,max_bytes=8388608))
    streaming = _feature_enabled(endpoint.features, 'streaming', default=True)
    payload = backend._chat_payload(messages, metadata, streaming=streaming,
                                    tools=tool_schemas(runtime, metadata['allowed_tools']))
    if policy.initial_tool is not None and payload.get('tools'):
        payload['tool_choice'] = {'type': 'function', 'function': {'name': policy.initial_tool}}
    native = encode_messages(payload) if endpoint.provider == 'anthropic' else payload
    return profile, endpoint, metadata, messages, native


def native_payload_digest(native):
    return hashlib.sha256(json.dumps(native,allow_nan=False).encode('utf-8')).hexdigest()


@tool_contract_preparation
def prepare_turn(runtime, artifact, *, message, history=(), input_data=None, policy=None,
                 limits=None, workspace=None, context_policy=None, token_counters=None,
                 context_selection=None, context_report=None, context_work=None, _warn_rules=True,
                 input_profile=None, authoritative_input_data=None, authoritative_input_profile=None,
                 authoritative_input_limits=None, _frozen_input=None, _input_variant_selection=None,
                 _input_data_variants=None, tool_contract_bindings=None):
    limits = limits if limits is not None else TurnLimits()
    if type(limits) is not TurnLimits:
        raise TurnAdmissionError('invalidPolicy')
    owned_history = validate_conversation_history(history, limits=limits)
    try:
        if type(artifact) is not ProsaicArtifact:
            raise ValueError('already inspected artifact required')
        # Rule keys appear only when inspection carried rules: untagged bytes are unchanged.
        raw_agent = artifact.inspection()
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
        ruleset = turn_rules(runtime, owned_agent, warn=_warn_rules)
    except RuleAdmissionError as exc:
        raise TurnAdmissionError(exc.code, detail=exc.detail) from None
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
        from .tool_contracts import admit_tool_contracts
        from .mcp import admit_mcp_tools
        admit_mcp_tools(runtime._custom_tools, names, surface="api", policy=policy,
                        contracts=tool_contract_bindings, initial_tool=policy.initial_tool)
        admit_tool_contracts(runtime._custom_tools, names, tool_contract_bindings)
        from .tool_parallel_contracts import admit_parallel_execution
        admit_parallel_execution(policy,tool_contract_bindings,metadata_limit=limits.max_metadata_bytes)
        if policy.parallel_tools is not None and limits.max_evidence_bytes < (1024 + tool_contract_bindings.limits.max_evidence_bytes + tool_contract_bindings.limits.max_citation_bytes + len(encode_owned(tool_contract_bindings.to_dict())) + policy.max_tool_calls*2560):
            raise ValueError('parallel evidence headroom required')
        from .terminal import admit_terminal_tools
        admit_terminal_tools(runtime._custom_tools, names, policy.allowed_terminal_tools, supported=False)
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
        if (limits.max_evidence_bytes < 1024 + (policy.max_tool_calls * 1536 if names else 0) or
                limits.max_metadata_bytes < 128 or limits.max_event_bytes < 128):
            raise ValueError('terminal evidence headroom required')
        if tool_contract_bindings is not None:
            contract_limits = tool_contract_bindings.limits
            reserve = contract_limits.max_evidence_bytes + contract_limits.max_citation_bytes + len(encode_owned(tool_contract_bindings.to_dict()))
            if limits.max_evidence_bytes < 1024 + reserve + (policy.max_tool_calls * 1536 if names else 0):
                raise ValueError('tool contract evidence headroom required')
            if limits.max_event_bytes < contract_limits.max_event_bytes:
                raise ValueError('tool contract event headroom required')
            if tool_contract_bindings.citations is not None and limits.max_metadata_bytes < 128 + contract_limits.max_citation_bytes:
                raise ValueError('citation metadata headroom required')
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
    from .input_views import bind_views
    views=bind_views(ruleset,data,input_profile=input_profile,
        authoritative_input_data=authoritative_input_data,authoritative_input_profile=authoritative_input_profile,
        authoritative_input_limits=authoritative_input_limits)
    if views is not None:
        # Reserve the receipt's additional identity data before any provider/tool work.
        overhead=len(encode_owned(views['inputViews']))
        if overhead+1024+(policy.max_tool_calls*1536 if names else 0)>limits.max_evidence_bytes:
            raise TurnAdmissionError('inputLimit','maxEvidenceBytes')
    try:
        selected_history = owned_history
        variant_payload=None
        report, selection, work = None, None, None
        if context_policy is None:
            if any(value is not None for value in (token_counters, context_selection, context_report, context_work)):
                raise TurnAdmissionError('invalidPolicy')
        else:
            from .context_counters import admit_counter
            from .context_assembly import assemble_context, restore_selection
            _, endpoint, _ = turn_endpoint(runtime, owned_agent, policy)
            registration = admit_counter(context_policy, token_counters, endpoint)
            def candidate(history):
                return initial_payload(runtime, owned_agent, policy, history, message, data,
                                       context_policy=context_policy,canonical_messages=views is not None or tool_contract_bindings is not None)[-1]
            if context_selection is not None:
                selected_history, report, selection, work = restore_selection(owned_history,
                    context_policy, registration, context_selection, context_report, context_work)
            else:
                if context_report is not None or context_work is not None:
                    raise TurnAdmissionError('invalidPolicy')
                if views is not None:
                    from .input_variants import fit_variants
                    selected_history,report,selection,work,data,variant_payload,variant_size,variant_native_digest,_input_variant_selection=fit_variants(
                        runtime,owned_agent,policy,limits,owned_history,message,
                        _input_data_variants if _input_data_variants is not None else [data],
                        context_policy,registration,input_profile)
                    views=bind_views(ruleset,data,input_profile=input_profile,
                        authoritative_input_data=authoritative_input_data,authoritative_input_profile=authoritative_input_profile,
                        authoritative_input_limits=authoritative_input_limits)
                else:
                    selected_history, report, selection, work = assemble_context(owned_history,
                        candidate, context_policy, registration,
                        replace(limits, max_input_bytes=policy.max_input_bytes))
        from .context_assembly import context_digest
        if variant_payload is not None:
            profile,endpoint,metadata,messages,native=variant_payload
            input_bytes=variant_size
            native_digest=variant_native_digest
        elif _frozen_input is None:
            profile, endpoint, metadata, messages, native = initial_payload(
                runtime, owned_agent, policy, selected_history, message, data,
                context_policy=context_policy,canonical_messages=views is not None or tool_contract_bindings is not None)
            # This is the exact byte encoding used by both existing transports.
            input_bytes = len(json.dumps(native, allow_nan=False).encode('utf-8'))
            native_digest=native_payload_digest(native) if views is not None or tool_contract_bindings is not None else None
        else:
            if views is None and tool_contract_bindings is None: raise TurnAdmissionError('bindingMismatch')
            profile,endpoint,metadata=prepared_endpoint(runtime,owned_agent,policy,context_policy)
            messages=initial_projection(owned_agent,selected_history,message,data,endpoint)
            input_bytes=_frozen_input['inputBytes']
            native_digest=_frozen_input['nativePayloadDigest']
        if input_bytes > policy.max_input_bytes:
            raise TurnAdmissionError('contextLimit', 'maxInputBytes')
        # Validate before copying; dataclasses.asdict recursively expands
        # aliases/cycles without the bounded JSON traversal.
        endpoint_identity = {f.name: getattr(endpoint, f.name) for f in fields(endpoint)}
        if endpoint.supported_efforts is not None:
            endpoint_identity['supported_efforts'] = list(endpoint.supported_efforts)
        endpoint_identity.pop('api_key_env', None)
        endpoint_identity.pop('api_key_file', None)
        material = {'agent': raw_agent, 'history': owned_history, 'message': message, 'inputData': data,
                    'policy': policy_document(policy), 'limits': asdict(limits), 'workspace': str(workspace) if workspace is not None else None,
                    'profile': profile, 'endpoint': endpoint_identity,
                    'tools': {n: runtime.tool_descriptors[n] for n in sorted(names - BUILTIN_TOOLS)},
                    'cliSandbox': {'mode': runtime.config.cli_sandbox.mode,
                                   'runtimeRoots': list(runtime.config.cli_sandbox.runtime_roots)},
                    'messages': messages}
        if not ruleset.empty:
            # Guard versions and effective bindings: readmission detects drift.
            material['rules'] = ruleset.material()
            if runtime._rule_retries != 1:
                material['rules']['ruleRetries'] = runtime._rule_retries  # Default turns keep their bytes.
        if runtime._correct_malformed_tool_calls:
            material['toolRequestCorrection'] = True  # Only when on: default turns keep their bytes.
        if tool_contract_bindings is not None:
            material['toolContractV1'] = tool_contract_bindings.to_dict()
            if policy.parallel_tools is not None:
                from .tool_parallel_contracts import parallel_binding
                material['parallelToolsV1']=parallel_binding(policy.parallel_tools)
            material['nativePayloadDigest'] = native_digest
        if views is not None:
            material.update(views)
            from .frozen_turns import InputVariantSelection
            variant_selection=_input_variant_selection
            if variant_selection is None:
                data_digest=context_digest(data)
                variant_selection=InputVariantSelection.from_dict(dict(version=1,inputProfile=input_profile,
                    candidateDigests=[data_digest],selectedIndex=0,selectedDigest=data_digest,selectionWorkLimited=False))
            if (type(variant_selection) is not InputVariantSelection or
                    variant_selection.to_dict()['selectedDigest']!=context_digest(data) or
                    variant_selection.to_dict()['inputProfile']!=input_profile):
                raise TurnAdmissionError('bindingMismatch')
            material.update(inputVariantSelection=variant_selection.to_dict(),nativePayloadDigest=native_digest)
        if context_policy is not None:
            if input_bytes != report.encoded_input_bytes:
                raise TurnAdmissionError('bindingMismatch')
            material.update({'selectedHistory': selected_history,
                'contextPolicy': context_policy.to_dict(),
                'counterDescriptor': registration.descriptor.to_dict(),
                'contextSelection': selection.to_dict(), 'contextReport': report.to_dict()})
        ownership_cap=limits.max_history_bytes+limits.max_input_bytes*2
        if views is not None and views['authoritativeInputLimits'] is not None:
            ownership_cap+=views['authoritativeInputLimits']['max_bytes']
        encoded = encode_owned(material, max_bytes=ownership_cap,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)
        identity_material = {k: v for k, v in material.items() if k != 'workspace'}
        identity = hashlib.sha256(encode_owned(identity_material,
            max_bytes=ownership_cap,
            max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)).hexdigest()
        prepared = PreparedTurn._create(encoded, identity, input_bytes,
            encoding_version='conversation-turn-v5' if policy.parallel_tools is not None else 'conversation-turn-v4' if tool_contract_bindings is not None else (
                'conversation-turn-v3' if views is not None else ('conversation-turn-v2' if context_policy is not None else 'conversation-turn-v1')),
            context_work=work)
        # Private compiled rules, reused by execution; never serialized or compared.
        object.__setattr__(prepared, '_rules', ruleset)
        if views is not None or tool_contract_bindings is not None:
            object.__setattr__(prepared,'_initial_context_work',work.to_dict() if work is not None else None)
            # Freezeability is an admission requirement, including combined
            # ownership/field bounds; it must not fail after an effect.
            prepared.export_frozen_input()
        return prepared
    except TurnAdmissionError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise TurnAdmissionError('invalidPolicy') from exc


def execution_snapshot(runtime, material):
    """Own the selected execution mapping before any invocation callbacks."""
    profile = material['profile']
    endpoint = replace(runtime.config.profiles[profile], **material['endpoint'])
    from .config import CliSandboxConfig
    sandbox = CliSandboxConfig(material['cliSandbox']['mode'], tuple(material['cliSandbox']['runtimeRoots']))
    tier = material['agent']['frontmatter'].get('model_tier')
    config = replace(runtime.config, profiles={profile: endpoint}, default_profile=profile,
                     routes={tier: profile} if tier is not None else {}, cli_sandbox=sandbox)
    return config, dict(runtime._custom_tools), dict(runtime._cli_tools)
