"""Native Anthropic Messages transport; tools execute only in the shared loop."""
import json
import socket
import time
import urllib.error
import urllib.request

from .accounting_capture import strict_json
from .execution import ExecutionBackend
from .provider_types import ProviderTurn
from .anthropic_usage import normalize_anthropic_usage
from .types import Result
from .tools import depth
from .openai_compatible import (
    OpenAICompatibleBackend, _OpenAIToolRegistry, _api_key, _feature_enabled,
    _http_status, _is_sse_response, _prompt_metadata, _raw_response_headers,
    _tool_call_summary,
)
from .openai_compatible_transcript import ProviderTranscript

API_VERSION = '2023-06-01'


def native_headers(token):
    headers = {'Content-Type': 'application/json', 'anthropic-version': API_VERSION}
    if token:
        headers['x-api-key'] = token
    return headers


def encode_messages(payload):
    """Convert the loop's internal history after acquisition/tool-choice edits."""
    messages, system = [], []
    for message in payload['messages']:
        role = message['role']
        if role == 'system':
            system.append(message['content'])
            continue
        if role == 'tool':
            content = message['content']
            block = {'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': content}
            if json.loads(content).get('status') == 'error':
                block['is_error'] = True
            role, blocks = 'user', [block]
        elif role == 'assistant' and 'provider_content' in message:
            blocks = message['provider_content']
        else:
            blocks = [{'type': 'text', 'text': message['content']}]
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(blocks)
        else:
            messages.append({'role': role, 'content': list(blocks)})
    native = {'model': payload['model'], 'max_tokens': payload['max_tokens'], 'messages': messages}
    if system:
        native['system'] = '\n\n'.join(system)
    if payload.get('temperature') is not None:
        native['temperature'] = payload['temperature']
    if payload.get('stream'):
        native['stream'] = True
    if payload.get('tools'):
        native['tools'] = [{'name': t['function']['name'],
                            'description': t['function'].get('description', ''),
                            'input_schema': t['function']['parameters']} for t in payload['tools']]
        choice = payload.get('tool_choice', 'auto')
        native['tool_choice'] = ({'type': 'tool', 'name': choice['function']['name'],
                                  'disable_parallel_tool_use': True}
                                 if isinstance(choice, dict) else {'type': 'auto'})
    return native


def usage_details(raw):
    usage = normalize_anthropic_usage(raw)
    if usage['status'] in {'untrusted', 'unknown'} or usage['total_tokens'] is None:
        return {}
    return {'prompt_tokens': usage['input_tokens'], 'completion_tokens': usage['output_tokens'],
            'total_tokens': usage['total_tokens']}


def parse_message(value, *, streamed=False, http_status=200, headers=None, usage_conflict=False):
    """Validate native content before exposing any executable tool request."""
    if (not isinstance(value, dict) or value.get('type') != 'message' or
            value.get('role') != 'assistant' or not isinstance(value.get('content'), list) or
            not isinstance(value.get('id'), str) or not value['id'] or
            not isinstance(value.get('model'), str) or not value['model']):
        raise ValueError('invalid Messages response')
    depth(value)
    text, calls, ids = [], [], set()
    for block in value['content']:
        if not isinstance(block, dict):
            raise ValueError('invalid content block')
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            text.append(block['text'])
        elif block.get('type') == 'tool_use':
            if (not isinstance(block.get('id'), str) or not block['id'] or block['id'] in ids or
                    not isinstance(block.get('name'), str) or not block['name'] or
                    type(block.get('input')) is not dict):
                raise ValueError('invalid tool_use block')
            ids.add(block['id'])
            calls.append({'id': block['id'], 'type': 'function', 'function': {
                'name': block['name'], 'arguments': json.dumps(block['input'], allow_nan=False)}})
        else:
            raise ValueError('unsupported content block')
    details = {} if usage_conflict else usage_details(value.get('usage'))
    raw = {k: value[k] for k in ('id', 'model', 'stop_reason', 'stop_sequence', 'usage') if k in value}
    raw['usage_status'] = 'untrusted' if usage_conflict else normalize_anthropic_usage(value.get('usage'))['status']
    metadata = {'provider': 'anthropic', 'streamed': streamed, 'http_status': http_status,
                'raw_response_headers': headers or {}, 'raw_response_metadata': raw,
                'token_usage_details': details}
    stop = value.get('stop_reason')
    if stop not in {'end_turn', 'stop_sequence', 'tool_use'}:
        code = 'incomplete_generation' if stop == 'max_tokens' else 'unsupported_stop_reason'
        return Result(1, ''.join(text), 'Anthropic did not complete the requested turn',
                      token_usage=details.get('total_tokens'), metadata={**metadata,
                      'provider_error_code': code, 'failure_reason': code, 'finish_reason': stop})
    if bool(calls) != (stop == 'tool_use'):
        raise ValueError('tool use and stop reason disagree')
    return ProviderTurn(''.join(text), 'tool_calls' if calls else 'stop', details.get('total_tokens', 0),
                        details, raw, False, calls, streamed, http_status, headers or {},
                        continuation=value['content'])


class AnthropicBackend(ExecutionBackend):
    name = 'anthropic'

    def __init__(self, config):
        self._config = config

    def make_registry(self, cwd, features, metadata):
        return _OpenAIToolRegistry(cwd, features, metadata)

    def tool_call_summary(self, call):
        return _tool_call_summary(call)

    def tool_event_metadata(self, name):
        return {}

    def strict_tool_names(self):
        return frozenset()

    def open_transcript(self, request):
        return ProviderTranscript(None)

    def tool_guidance(self):
        return OpenAICompatibleBackend.tool_guidance(self)

    def run_prompt(self, request):
        return self._run_prompt_with_tools(request, _prompt_metadata(request),
                                           _feature_enabled(self._config.features, 'streaming', default=True))

    def _chat_payload(self, messages, prompt_metadata, *, streaming, tools=None):
        unsupported = {'json_mode', 'reasoning_effort', 'effort', 'thinking', 'stream_options', 'web_tools'}
        for name in unsupported:
            value = self._config.features.get(name)
            if value not in (None, False, 'off', 'false', 'disabled'):
                raise ValueError(f'unsupported Anthropic feature: {name}')
        if any(prompt_metadata.get(k) is not None for k in ('effort', 'reasoning_effort')):
            raise ValueError('unsupported Anthropic effort control')
        payload = OpenAICompatibleBackend._chat_payload(self, messages, prompt_metadata, streaming=streaming, tools=tools)
        if type(payload.get('max_tokens')) is not int or payload['max_tokens'] <= 0:
            raise ValueError('Anthropic max_tokens must be a positive integer')
        payload.pop('stream_options', None)
        return payload

    def _post_chat_turn(self, payload, request, deadline, streaming):
        native = encode_messages(payload)
        self.validate_payload(native)
        token, error = _api_key(self._config.api_key_env, self._config.api_key_file, request.env)
        if error:
            return Result(1, '', error, metadata={'provider': self.name, 'provider_error_code': 'api_key_file_error'})
        http_request = urllib.request.Request(self._config.base_url.rstrip('/') + '/messages',
            data=json.dumps(native, allow_nan=False).encode('utf-8'), headers=native_headers(token), method='POST')
        try:
            with self.open_http(http_request, timeout=max(0.001, min(request.timeout_s, deadline - time.monotonic()))) as response:
                status, headers = _http_status(response), _raw_response_headers(response)
                if streaming and _is_sse_response(response):
                    return self._read_sse_turn(response, deadline)
                body = self.read_response(response)
        except (TimeoutError, socket.timeout):
            return Result(1, '', 'Anthropic request timed out', timed_out=True,
                          metadata={'provider': self.name, 'provider_error_code': 'timeout'})
        except UnicodeError:
            return Result(1, '', 'Malformed Anthropic response', metadata={
                'provider': self.name, 'provider_error_code': 'malformed_response'})
        except urllib.error.HTTPError as exc:
            exc.close()
            return Result(exc.code, '', 'Anthropic request rejected', metadata={
                'provider': self.name, 'http_status': exc.code, 'provider_error_code': 'http_error'})
        except (urllib.error.URLError, OSError) as exc:
            return Result(1, '', 'Anthropic transport failed', metadata={
                'provider': self.name, 'provider_error_code': 'url_error'})
        try:
            return parse_message(strict_json(body), http_status=status, headers=headers)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return Result(1, '', 'Malformed or unsupported Anthropic response', metadata={
                'provider': self.name, 'http_status': status, 'provider_error_code': 'malformed_response'})

    def _read_sse_turn(self, response, deadline):
        # Implemented separately from JSON framing; partial tool inputs are never executable.
        from .anthropic_stream import read_anthropic_turn
        return read_anthropic_turn(response, deadline)
