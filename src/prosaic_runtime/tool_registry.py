"""Compose custom dispatch with builtin path-scoped tools; registration is no grant."""
import json
from .tools import execute_custom_tool, parse_arguments
from .operation_context import bounded_reference
from jsonschema.exceptions import ValidationError


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
                 execution_options=None):
        self.builtin = builtin_registry
        self.custom = custom_tools
        self.allowed = frozenset(allowed_tools)
        self.check_boundary = check_boundary
        self.before_tool = before_tool
        self.execution_options = execution_options

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

    def execute_message(self, tool_call):
        self.check_boundary()
        if self.before_tool is not None:
            self.before_tool()
        function = tool_call.get('function')
        name = function.get('name') if isinstance(function, dict) else None
        if isinstance(name, str) and name not in self.custom:
            return self.builtin.execute_message(tool_call)
        payload = {'status': 'error', 'error': 'not_granted'}
        if isinstance(name, str) and name in self.allowed and tool_call.get('type') == 'function':
            payload = execute_custom_tool(self.custom[name], function.get('arguments'),
                check_boundary=self.check_boundary,
                **(self.execution_options() if self.execution_options is not None else {}))
        return {'role': 'tool', 'tool_call_id': str(tool_call.get('id') or ''),
                'content': json.dumps(payload, allow_nan=False)}
