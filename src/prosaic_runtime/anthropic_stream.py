"""Strict native SSE assembly; tool blocks leave this module only when complete."""
import time

from .accounting_capture import strict_json
from .anthropic import parse_message, usage_details
from .events import check_cancelled, print
from .openai_compatible import _http_status, _raw_response_headers
from .types import Result


class StreamState:
    def __init__(self, on_text):
        self.message = None
        self.blocks = []
        self.active = None
        self.arguments = ''
        self.delta_seen = False
        self.stopped = False
        self.on_text = on_text

    def push(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise ValueError('invalid stream event')
        kind = event['type']
        if self.stopped:
            raise ValueError('event after message_stop')
        if kind == 'ping':
            return
        if kind == 'message_start':
            message = event.get('message')
            if (self.message is not None or not isinstance(message, dict) or
                    message.get('type') != 'message' or message.get('role') != 'assistant' or
                    message.get('content') != [] or message.get('stop_reason') is not None or
                    not isinstance(message.get('id'), str) or not message['id'] or
                    not isinstance(message.get('model'), str) or not message['model']):
                raise ValueError('invalid message_start')
            self.message = {**message, 'content': self.blocks}
            return
        if self.message is None:
            raise ValueError('missing message_start')
        if kind == 'content_block_start':
            index, block = event.get('index'), event.get('content_block')
            if (self.delta_seen or self.active is not None or type(index) is not int or
                    index != len(self.blocks) or not isinstance(block, dict)):
                raise ValueError('invalid block start')
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                if block['text']:
                    self.on_text(block['text'])
            elif block.get('type') == 'tool_use' and type(block.get('input')) is dict:
                if block['input']:
                    raise ValueError('stream tool input must start empty')
            else:
                raise ValueError('unsupported content block')
            self.blocks.append(dict(block))
            self.active, self.arguments = index, ''
            return
        if kind in {'content_block_delta', 'content_block_stop'}:
            index = event.get('index')
            if type(index) is not int or self.active is None or index != self.active or self.delta_seen:
                raise ValueError('inactive block')
            block = self.blocks[index]
            if kind == 'content_block_stop':
                if block['type'] == 'tool_use' and self.arguments:
                    value = strict_json(self.arguments)
                    if type(value) is not dict:
                        raise ValueError('tool input must be an object')
                    block['input'] = value
                self.active, self.arguments = None, ''
                return
            delta = event.get('delta')
            if not isinstance(delta, dict):
                raise ValueError('invalid delta')
            if (block['type'] == 'text' and delta.get('type') == 'text_delta' and
                    isinstance(delta.get('text'), str)):
                block['text'] += delta['text']
                if delta['text']:
                    self.on_text(delta['text'])
            elif (block['type'] == 'tool_use' and delta.get('type') == 'input_json_delta' and
                    isinstance(delta.get('partial_json'), str)):
                self.arguments += delta['partial_json']
            else:
                raise ValueError('unsupported block delta')
            return
        if kind == 'message_delta':
            delta = event.get('delta')
            if self.active is not None or not isinstance(delta, dict):
                raise ValueError('invalid message delta')
            if set(delta) - {'stop_reason', 'stop_sequence'}:
                raise ValueError('unsupported message delta')
            previous = self.message.get('stop_reason')
            stop = delta.get('stop_reason')
            if previous is not None and stop is not None and previous != stop:
                raise ValueError('conflicting stop reason')
            self.message.update(delta)
            usage = event.get('usage')
            if usage is not None:
                if not isinstance(usage, dict) or not isinstance(self.message.get('usage', {}), dict):
                    raise ValueError('invalid usage')
                self.message['usage'] = {**self.message.get('usage', {}), **usage}
            self.delta_seen = True
            return
        if kind == 'message_stop':
            if self.active is not None or not self.delta_seen or self.message.get('stop_reason') is None:
                raise ValueError('incomplete message')
            self.stopped = True
            return
        # Unknown consequential events cannot be assumed to preserve execution semantics.
        raise ValueError('unsupported stream event')


def read_anthropic_turn(response, deadline, *, on_text=print):
    state = StreamState(on_text)
    data, event_name = [], None
    status, headers = _http_status(response), _raw_response_headers(response)

    def flush():
        nonlocal data, event_name
        if data:
            event = strict_json('\n'.join(data))
            if event_name is not None and (not isinstance(event, dict) or event.get('type') != event_name):
                raise ValueError('SSE event name mismatch')
            state.push(event)
        elif event_name is not None:
            raise ValueError('SSE event missing data')
        data, event_name = [], None

    try:
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('stream deadline')
            # Reset the socket timeout to the remaining invocation budget before each read.
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(max(0.001, remaining))
            raw = response.readline()
            if not raw:
                if data or event_name is not None:
                    raise ValueError('unterminated SSE event')
                break
            line = raw.decode('utf-8', errors='strict').rstrip('\r\n')
            if not line:
                flush()
            elif line.startswith(':'):
                continue
            else:
                field, separator, value = line.partition(':')
                if value.startswith(' '):
                    value = value[1:]
                if field == 'event':
                    if event_name is not None:
                        raise ValueError('duplicate SSE event name')
                    event_name = value
                elif field == 'data':
                    data.append(value if separator else '')
        if not state.stopped:
            raise ValueError('missing message_stop')
        turn = parse_message(state.message, streamed=True, http_status=status, headers=headers)
        if not isinstance(turn, Result):
            turn.previewed = True
        return turn
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        # Size-limit exceptions are host guards, not malformed provider messages.
        from .runtime import LimitExceeded
        if isinstance(exc, LimitExceeded):
            raise
        message = state.message or {}
        return Result(1, '', 'Malformed or unsupported Anthropic stream', metadata={
            'provider': 'anthropic', 'provider_error_code': 'malformed_response', 'streamed': True,
            'http_status': status, 'raw_response_metadata': {
                k: message[k] for k in ('id', 'model', 'usage', 'stop_reason') if k in message}})
