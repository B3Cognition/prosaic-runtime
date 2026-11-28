"""Synthetic tools and a scripted provider for constrained tool-request correction.

The scripted provider stands in for a weak model: on the correction request it repeats its malformed call,
unless the request carries a JSON-schema constraint, in which case it answers with content that fits it, as
a constrained decoder would. Every name, value and schema here is synthetic.
"""

from dataclasses import replace
import json

from prosaic_runtime import CustomTool, ProsaicRuntime

FEATURE = 'constrained_tool_correction'
WIRES = ('json_schema', 'omlx_grammar')

FIND_STATION = {
    'type': 'object',
    'properties': {
        'query': {'type': 'string', 'maxLength': 64, 'description': 'Synthetic station name'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5},
    },
    'required': ['query', 'limit'],
    'additionalProperties': False,
}
READ_GAUGE = {
    'type': 'object',
    'properties': {
        'station': {'type': 'string', 'pattern': '^ST-[0-9]{3}$'},
        'unit': {'type': 'string', 'enum': ['metres', 'feet']},
        'window': {
            'type': 'object',
            'properties': {'start': {'type': 'string', 'format': 'date'}, 'hours': {'type': 'integer', 'minimum': 1}},
            'required': ['start', 'hours'],
            'additionalProperties': False,
        },
    },
    'required': ['station', 'unit', 'window'],
    'additionalProperties': False,
}
SCHEMAS = {'find_station': FIND_STATION, 'read_gauge': READ_GAUGE}
RESULTS = {'find_station': {'station': 'ST-104', 'name': 'North Pier'}, 'read_gauge': {'level_m': 3.42}}
VALID = {
    'find_station': {'query': 'North Pier', 'limit': 1},
    'read_gauge': {'station': 'ST-104', 'unit': 'metres', 'window': {'start': '2026-01-05', 'hours': 6}},
}


class Tools:
    """Records every authorisation and read, so a test can count exactly one authorised read."""

    def __init__(self, fx, *, authorize=True, names=('find_station', 'read_gauge'), schemas=None):
        self.authorized, self.reads = [], []
        for name in names:
            schema = (schemas or SCHEMAS)[name]
            fx.tools[name] = CustomTool(
                name,
                f'Synthetic {name}',
                schema,
                lambda args, name=name: self.reads.append((name, args)) or RESULTS.get(name, {'ok': True}),
                'v1',
                authorize=lambda args, name=name: self.authorized.append((name, args)) or authorize,
            )


def runtime(fx, wire=None, *, guards=(), **features):
    """A correcting runtime; ``wire`` selects the constraint, None keeps today's ordinary correction."""
    config = fx.runtime.config
    endpoint = config.profiles['local']
    extra = {FEATURE: wire} if wire is not None else {}
    profile = replace(endpoint, features={**endpoint.features, 'streaming': False, **extra, **features})
    return ProsaicRuntime(
        replace(config, profiles={'local': profile}),
        custom_tools=fx.tools,
        guards=list(guards),
        correct_malformed_tool_calls=True,
    )


def constraint(request):
    """The JSON schema a request constrains output to, on either wire, or None."""
    response_format = request.get('response_format')
    if isinstance(response_format, dict) and response_format.get('type') == 'json_schema':
        return response_format['json_schema']['schema']
    structured = request.get('structured_outputs')
    if isinstance(structured, dict) and 'json' in structured:
        return structured['json']
    return None


def completion(message, finish_reason):
    body = {
        'id': 'synthetic',
        'model': 'fixture-model',
        'choices': [{'message': {'role': 'assistant', **message}, 'finish_reason': finish_reason}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
    }
    return 200, 'application/json', json.dumps(body).encode()


def calls(*items):
    return completion(
        {
            'content': '',
            'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}
                for call_id, name, arguments in items
            ],
        },
        'tool_calls',
    )


def text(content):
    return completion({'content': content}, 'stop')


def scripted(fx, malformed, resend, *, answer=None):
    """Queue the three-request shape: malformed attempt, correction, grounded answer.

    ``malformed`` is ``(name, arguments)``; ``resend`` is the ``{"tool", "arguments"}`` a constrained decoder
    emits. Without a constraint the weak model repeats its malformed call.
    """
    name, arguments = malformed
    fx.responses.append(calls(('first', name, arguments)))

    def correction(request):
        if constraint(request) is None:
            return calls(('again', name, arguments))
        return text(json.dumps(resend))

    def grounded(request):
        if answer is not None:
            return text(answer)
        result = json.loads(request['messages'][-1]['content'])
        return text(f'Grounded in the read: {json.dumps(result, sort_keys=True)}')

    fx.responses.extend([correction, grounded])
