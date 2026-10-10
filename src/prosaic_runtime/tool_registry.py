"""Compose custom dispatch with builtin path-scoped tools; registration is no grant."""
import json
from .tools import execute_custom_tool


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
