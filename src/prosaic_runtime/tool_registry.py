"""Compose custom dispatch with builtin path-scoped tools; registration is no grant."""
import json
from .tools import execute_custom_tool, parse_arguments
from .operation_context import bounded_reference
from jsonschema.exceptions import ValidationError
from .tool_outcomes import ToolOutcome
from .tool_effects import ToolEffectFailure


class InvalidToolRequest(ValueError):
    """A model batch failed pure admission; no partial dispatch is permitted."""
    def __init__(self):
        super().__init__('invalidToolRequest')


class DisabledBuiltinRegistry:
    """No root or filesystem mapping exists for a workspace-free turn."""
    def openai_tools(self):
        return []

    def execute_message(self, tool_call):
        return {'role': 'tool', 'tool_call_id': str(tool_call.get('id') or ''),
                'content': json.dumps({'status': 'error', 'error': 'not_granted'})}


class BoundedToolRegistry:
    def __init__(self, builtin_registry, custom_tools, allowed_tools, check_boundary, before_tool=None,
                 execution_options=None, *, index_all_calls=False, bounded_recovery=False,
                 terminal_capture=None, terminal_tools=()):
        self.builtin = builtin_registry
        self.custom = custom_tools
        self.allowed = frozenset(allowed_tools)
        self.check_boundary = check_boundary
        self.before_tool = before_tool
        self.execution_options = execution_options
        self.index_all_calls = index_all_calls
        self.bounded_recovery = bounded_recovery
        self.terminal_capture = terminal_capture
        self.terminal_tools = frozenset(terminal_tools) & self.allowed
        self.turn = 0
        self.last_outcome = ToolOutcome('unclassified')

    def _outcome(self, outcome):
        self.last_outcome = ToolOutcome.from_dict(outcome.to_dict())

    def _finish(self, message):
        evidence = self.last_outcome.to_dict()
        if (self.bounded_recovery or self.terminal_tools) and evidence['recovery'] in {'stop', 'reconcile'}:
            raise ToolEffectFailure('tool_effect_uncertain' if evidence['effect']=='unknown' else 'tool_recovery_blocked')
        return message

    def openai_tools(self):
        return self.builtin.openai_tools() + [
            {'type': 'function', 'function': {'name': tool.name,
             'description': tool.description, 'parameters': tool.parameters}}
            for name, tool in sorted(self.custom.items()) if name in self.allowed]

    def validate_batch(self, calls):
        """Validate the entire wire batch without effect hooks or counters."""
        schemas = {s['function']['name']: s['function']['parameters'] for s in self.openai_tools()}
        ids, admitted = set(), []
        try:
            if type(calls) not in (list, tuple):
                raise ValueError('batch shape')
            if len(calls) > 1 and any(type(c) is dict and type(c.get('function')) is dict
                    and c['function'].get('name') in self.terminal_tools for c in calls):
                raise ValueError('terminal tools require a singleton batch')
            for call in calls:
                if type(call) is not dict or set(call) != {'id', 'type', 'function'}:
                    raise ValueError('call shape')
                if not bounded_reference(call['id'], 128) or call['id'] in ids or call['type'] != 'function':
                    raise ValueError('call identity or type')
                function = call['function']
                if type(function) is not dict or set(function) != {'name', 'arguments'}:
                    raise ValueError('function shape')
                name = function['name']
                if type(name) is not str or name not in self.allowed or name not in schemas:
                    raise ValueError('ungranted tool')
                maximum = self.custom[name].max_argument_bytes if name in self.custom else 16_384
                parse_arguments(function['arguments'], schemas[name], maximum)
                ids.add(call['id'])
                admitted.append({'id': call['id'], 'type': 'function',
                                 'function': dict(function)})
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, ValidationError):
            raise InvalidToolRequest() from None
        return tuple(admitted)

    def _admit_attempt(self, tool_call):
        """Shared preamble: boundary, attempt budget and the call's execution index."""
        self.check_boundary()
        if self.before_tool is not None:
            self.before_tool()
        function = tool_call.get('function')
        name = function.get('name') if isinstance(function, dict) else None
        options = (self.execution_options() if self.execution_options is not None and
                   (self.index_all_calls or name in self.custom) else {})
        return name, options

    def refuse_message(self, tool_call, payload):
        """A host rule refused this call: it counts and is indexed like a denied call, but never runs."""
        self.last_outcome = ToolOutcome('not_granted')
        _, options = self._admit_attempt(tool_call)
        message = {'role': 'tool', 'tool_call_id': str(tool_call.get('id') or ''),
                   'content': json.dumps(payload, ensure_ascii=False, allow_nan=False)}
        if self.index_all_calls and callable(options.get('on_completion')):
            options['on_completion'](json.loads(message['content']))
        return message

    def execute_message(self, tool_call):
        self.last_outcome = ToolOutcome('unclassified')
        name, options = self._admit_attempt(tool_call)
        options['_event_call_id'] = None
        options['_event_exchange'] = self.turn
        from .tool_event_contract import call_identity, current_bindings
        if current_bindings() is not None:
            # Neutral turns already allocated a stable ID for this exact call.
            owner = getattr(self.execution_options, '__self__', None)
            capture = getattr(owner, 'turn_capture', None)
            options['_event_call_id'] = (capture.call_ids[tool_call['id']] if capture is not None else
                call_identity(current_bindings().scope, self.turn, options.get('call_index', 0)))
        function = tool_call.get('function')
        if isinstance(name, str) and name not in self.custom:
            message = self.builtin.execute_message(tool_call)
            self.last_outcome = ToolOutcome('not_granted' if name not in self.allowed else
                'ok' if json.loads(message['content']).get('status') == 'ok' else 'unclassified')
            if self.index_all_calls and callable(options.get('on_completion')):
                options['on_completion'](json.loads(message['content']))
            return self._finish(message)
        payload = {'status': 'error', 'error': 'not_granted'}
        self.last_outcome = ToolOutcome('not_granted')
        if isinstance(name, str) and name in self.allowed and tool_call.get('type') == 'function':
            if name in self.terminal_tools:
                options['on_terminal'] = lambda value, arguments: self.terminal_capture.capture(
                    value, tool=self.custom[name], arguments=arguments, scope=options['scope'],
                    call_id=tool_call['id'], turn=self.turn)
            payload = execute_custom_tool(self.custom[name], function.get('arguments'),
                check_boundary=self.check_boundary,
                on_outcome=self._outcome, **options)
        return self._finish({'role': 'tool', 'tool_call_id': str(tool_call.get('id') or ''),
                'content': json.dumps(payload, allow_nan=False)})
