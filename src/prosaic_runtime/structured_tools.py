"""Explicit structured-JSON tool transport; applications retain final-result admission.

This driver does not call a model, select an agent, retry provider calls or write run
state. A host/Harness supplies one Runtime Result per yield. No native-to-JSON fallback.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import time
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError

from .events import Cancelled
from .operation_context import InvocationScope
from .tool_effects import admit_tool_effects, ToolEffectFailure
from .telemetry import ObserverEmitter
from .tools import (ToolDeadlineExceeded, depth, execute_custom_tool, parse_arguments, reject_constant,
                    unique_pairs, validate_custom_tools)


class StructuredToolError(RuntimeError):
    """Stable, public-safe failure code, without model/handler input or exception text."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Completion:
    value: Any


class StructuredToolLoop:
    def __init__(self, tools, *, arguments, deadline, allowed_tools=(), max_turns=12,
                 max_input_bytes=196608, max_response_bytes=131072, max_history_bytes=131072,
                 max_identical_calls=1, cancelled=lambda: False, on_event=lambda event: None,
                 operation_context=None, tool_journal=None, observer=None):
        self.tools = validate_custom_tools(tools)
        self.allowed_tools = frozenset(allowed_tools)
        if self.allowed_tools - self.tools.keys():
            raise ValueError('granted tools must be registered')
        self.operation_context = InvocationScope(str(uuid4())) if operation_context is None else operation_context
        admit_tool_effects(self.tools, self.allowed_tools, self.operation_context, tool_journal)
        self.tool_journal = tool_journal
        self.emitter = ObserverEmitter(observer, source='runtime', scope=self.operation_context)
        self.emitter._registered_tools = frozenset(self.tools)
        self.call_index = 0
        for value in (max_turns, max_input_bytes, max_response_bytes, max_history_bytes, max_identical_calls):
            if type(value) is not int or value <= 0:
                raise ValueError('limits must be positive integers')
        if not isinstance(deadline, (int, float)) or not float('-inf') < deadline < float('inf'):
            raise ValueError('finite monotonic deadline required')
        self.arguments = self._snapshot(arguments)
        if type(self.arguments) is not dict:
            raise ValueError('arguments must be an object')
        self.deadline, self.cancelled, self.on_event = deadline, cancelled, on_event
        self.max_turns, self.max_input_bytes = max_turns, max_input_bytes
        self.max_response_bytes, self.max_history_bytes = max_response_bytes, max_history_bytes
        self.max_identical_calls = max_identical_calls
        self.history, self.calls = [], {}
        self.used = False

    @staticmethod
    def _snapshot(value):
        depth(value)
        return json.loads(json.dumps(value, allow_nan=False))

    def check_boundary(self):
        if self.cancelled():
            raise StructuredToolError('execution-cancelled')
        if time.monotonic() >= self.deadline:
            raise StructuredToolError('execution-timeout')

    def parse(self, raw):
        if type(raw) is not str or len(raw.encode('utf-8')) > self.max_response_bytes:
            raise ValueError('response limit')
        value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        # Reject over-depth and exponent overflow (1e999), including proposal values.
        self._snapshot(value)
        if type(value) is not dict or set(value) not in ({'tool'}, {'proposal'}):
            raise ValueError('invalid envelope')
        if type(next(iter(value.values()))) is not dict:
            raise ValueError('invalid payload')
        return value

    def append(self, value):
        history = self.history + [self._snapshot(value)]
        if len(json.dumps(history).encode('utf-8')) > self.max_history_bytes:
            raise StructuredToolError('history-limit')
        self.history = history

    def dispatch(self, request):
        self.check_boundary()
        if type(request.name) is not str or request.name not in self.allowed_tools:
            raise ValueError('tool not granted')
        tool = self.tools[request.name]
        raw = json.dumps(request.arguments, allow_nan=False)
        # Validate before any execution or progress callback.
        args = parse_arguments(raw, tool.parameters, tool.max_argument_bytes)
        key = (request.name, json.dumps(args, sort_keys=True, allow_nan=False))
        if self.calls.get(key, 0) >= self.max_identical_calls:
            raise StructuredToolError('repeated-tool-call')
        self.calls[key] = self.calls.get(key, 0) + 1
        self.on_event({'type': 'tool_start', 'name': request.name})
        self.emitter.emit('tool_started', name=request.name)
        index = self.call_index
        self.call_index += 1
        try:
            outcome = execute_custom_tool(tool, raw, check_boundary=self.check_boundary,
                scope=self.operation_context, deadline=self.deadline, cancelled=self.cancelled,
                call_index=index, tool_journal=self.tool_journal)
        except ToolEffectFailure as exc:
            raise StructuredToolError(exc.reason) from None
        except ToolDeadlineExceeded:
            raise StructuredToolError('execution-timeout') from None
        except Cancelled:
            raise StructuredToolError('execution-cancelled') from None
        if outcome['status'] != 'ok':
            raise StructuredToolError('tool-' + outcome['error'])
        self.on_event({'type': 'tool_complete', 'name': request.name})
        self.emitter.emit('tool_completed', name=request.name, status='ok')
        return {'name': request.name, 'arguments': args}, outcome['result']

    def turns(self, *, admit, tool_outcome=lambda name, data: None, feedback=None):
        """Yield bounded JSON arguments, accepting Runtime Results via generator.send.

        admit(proposal) returns Completion(value), ToolRequest for a prerequisite,
        or raises ValueError to request one correction. tool_outcome may terminate
        with Completion. These callbacks are host authority, never model code.
        """
        if self.used:
            raise ValueError('loop is single-use')
        self.used = True
        corrected = False
        descriptors = [tool.descriptor for name, tool in sorted(self.tools.items())
                       if name in self.allowed_tools]
        for _ in range(self.max_turns):
            self.check_boundary()
            prompt = json.dumps({**self.arguments, 'availableTools': descriptors, 'history': self.history},
                                allow_nan=False)
            if len(prompt.encode('utf-8')) > self.max_input_bytes:
                raise StructuredToolError('input-limit')
            result = yield prompt
            self.check_boundary()
            if result.exit_code != 0:
                raise StructuredToolError('execution-timeout' if result.timed_out else 'execution-failed')
            step = None
            try:
                step = self.parse(result.stdout)
                if 'proposal' in step:
                    self.on_event({'type': 'proposal_validation'})
                    decision = admit(deepcopy(step['proposal']))
                    if isinstance(decision, Completion):
                        self.check_boundary()
                        return decision.value
                    if not isinstance(decision, ToolRequest):
                        raise TypeError('host admission must return Completion or ToolRequest')
                else:
                    call = step['tool']
                    if set(call) != {'name', 'arguments'}:
                        raise ValueError('invalid tool request')
                    decision = ToolRequest(call['name'], call['arguments'])
                call, data = self.dispatch(decision)
            except (ValueError, ValidationError, StructuredToolError) as error:
                reason = error.reason if isinstance(error, StructuredToolError) else 'invalid-structured-step'
                if reason not in {'invalid-structured-step', 'repeated-tool-call'}:
                    raise
                if corrected:
                    raise StructuredToolError(reason) from None
                corrected = True
                self.on_event({'type': 'correction', 'reason': reason})
                message = ('This tool call was already observed. Use its result in history and return '
                           'a proposal or a different necessary tool request.') if reason == 'repeated-tool-call' else (
                           feedback(deepcopy(step)) if feedback and step else
                           'Return exactly one valid tool request or proposal using registered tool schemas.')
                self.append({'validation': {'ok': False, 'message': message}})
                continue
            terminal = tool_outcome(decision.name, deepcopy(data))
            if terminal is not None:
                if not isinstance(terminal, Completion):
                    raise TypeError('host tool outcome must return Completion or None')
                self.check_boundary()
                return terminal.value
            self.append({'tool': call, 'result': data})
        raise StructuredToolError('step-limit')
