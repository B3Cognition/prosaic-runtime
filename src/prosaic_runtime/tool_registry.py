"""Compose custom dispatch with builtin path-scoped tools; registration is no grant."""
import json
from .tools import execute_custom_tool, parse_arguments
from .operation_context import bounded_reference
from jsonschema.exceptions import ValidationError
from .tool_outcomes import ToolOutcome
from .tool_effects import ToolEffectFailure


class InvalidToolRequest(ValueError):
    """A model batch failed pure admission; no partial dispatch is permitted.

    Batch admission names the call-level ``issue`` with a closed code, the call's position and, only
    when the call named a granted tool, that tool; nothing model-supplied. Other sources leave them unset.
    """
    def __init__(self, issue=None, index=None, tool=None):
        super().__init__('invalidToolRequest')
        self.issue, self.index, self.tool = issue, index, tool


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
        issue, index, tool = 'batchShape', None, None  # Closed codes: see tool_request_correction.ISSUES.
        try:
            if type(calls) not in (list, tuple):
                raise ValueError('batch shape')
            if len(calls) > 1 and any(type(c) is dict and type(c.get('function')) is dict
                    and c['function'].get('name') in self.terminal_tools for c in calls):
                issue = 'terminalBatch'
                raise ValueError('terminal tools require a singleton batch')
            for index, call in enumerate(calls):
                issue = 'callShape'
                if type(call) is not dict or set(call) != {'id', 'type', 'function'}:
                    raise ValueError('call shape')
                issue = 'callIdentity'
                if not bounded_reference(call['id'], 128) or call['id'] in ids or call['type'] != 'function':
                    raise ValueError('call identity or type')
                issue = 'functionShape'
                function = call['function']
                if type(function) is not dict or set(function) != {'name', 'arguments'}:
                    raise ValueError('function shape')
                issue = 'toolNotGranted'
                name = function['name']
                if type(name) is not str or name not in self.allowed or name not in schemas:
                    raise ValueError('ungranted tool')
                tool, issue = name, 'argumentsMalformed'
                maximum = self.custom[name].max_argument_bytes if name in self.custom else 16_384
                raw = function['arguments']
                if type(raw) is str and len(raw.encode('utf-8', 'surrogatepass')) > maximum:
                    issue = 'argumentsTooLarge'
                try:
                    parse_arguments(raw, schemas[name], maximum)
                except ValidationError:
                    issue = 'argumentsSchema'
                    raise
                ids.add(call['id'])
                admitted.append({'id': call['id'], 'type': 'function',
                                 'function': dict(function)})
                tool = None
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, ValidationError):
            raise InvalidToolRequest(issue, index, tool) from None
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
        options.setdefault('_event_call_id', None)
        options['_event_exchange'] = self.turn
        options['_defer_event'] = True
        from .tool_event_contract import call_identity, current_bindings
        if current_bindings() is not None and options['_event_call_id'] is None:
            # Neutral turns already allocated a stable ID for this exact call.
            owner = getattr(self.execution_options, '__self__', None)
            capture = getattr(owner, 'turn_capture', None)
            options['_event_call_id'] = (capture.call_ids[tool_call['id']] if capture is not None else
                call_identity(current_bindings().scope, self.turn, options.get('call_index', 0)))
        self.last_contract_call_id = options['_event_call_id']
        self.last_contract_call_index = options.get('call_index', 0)
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


class ParallelRegistryDispatch:
    """Call-local frames adapt the existing registry to the owner coordinator."""
    def __init__(self,registry,backend,hooks):
        from .tool_event_contract import current_bindings
        from .tool_effects import canonical_json
        import hashlib
        self.registry,self.backend,self.hooks=registry,backend,hooks
        self.custom=registry.custom;self.bounded_recovery=registry.bounded_recovery
        self.scope=current_bindings().scope;self.base_index=backend.tool_call_index
        self.batch_id=hashlib.sha256(canonical_json([self.scope.invocation_id,registry.turn]).encode()).hexdigest()
        self.frames={};self.options={}

    def identity(self,call,index):
        from .tool_event_contract import call_identity
        capture=self.backend.turn_capture
        return (capture.call_ids[call['id']] if capture is not None else
            call_identity(self.scope,self.registry.turn,self.base_index+index),self.base_index+index)

    def is_rule_refusal(self,index):return self.frames[index]['refusal'] is not None

    def _frame(self,call,permit,index,cancelled):
        permit.consume(self.backend.budget)
        self.registry.check_boundary()
        options=self.backend.tool_execution_options()
        ident,call_index=self.identity(call,index)
        if options['call_index']!=call_index:raise ValueError('parallel call index mismatch')
        frame=dict(call=call,outcome=ToolOutcome('unclassified'),id=ident,index=call_index,
            refusal=self.hooks.refusal(call) if self.hooks is not None else None,state={},manager=None,owner_cancelled=options.get('cancelled',lambda:False))
        def report(outcome):frame['outcome']=outcome
        complete=options.get('on_completion')
        def capture(outcome,status='completed',**refs):
            frame['state'].update(status=status,code=frame['outcome'].code,**refs)
            if complete is not None:complete(outcome,status,**refs)
        options.update(check_boundary=self.registry.check_boundary,cancelled=cancelled,
            on_completion=capture,on_outcome=report,_parallel_mode=True,_phase_state=frame['state'],
            _event_call_id=ident,_event_exchange=self.registry.turn)
        self.frames[index]=frame;self.options[index]=options
        return frame,options

    def _bare_start(self,frame,tool):
        from .events import emit
        emit('tool_started',name=tool.name,call_id=frame['call']['id'],turn=self.registry.turn)

    def prepare(self,call,*,permit,batch_index,cancelled,await_operation):
        from .tool_dispatch import prepare_custom_tool_call,WorkerSettlementV1
        from .tool_event_contract import ManagedToolCallV1
        frame,options=self._frame(call,permit,batch_index,cancelled)
        if frame['refusal'] is not None:
            frame['outcome']=ToolOutcome('not_granted');options['on_completion'](frame['refusal'])
            return WorkerSettlementV1(frame['id'],frame['index'],batch_index,'not_granted',
                json.dumps(frame['refusal'],ensure_ascii=False,allow_nan=False).encode())
        tool=self.custom[call['function']['name']]
        options['_await_operation']=await_operation
        options['_before_handler']=lambda args:self._bare_start(frame,tool)
        managed=ManagedToolCallV1(tool,call['function']['arguments'],options,call_id=frame['id'],
            exchange=self.registry.turn,defer_event=True,parallel=True)
        frame['manager']=managed
        try:
            result=prepare_custom_tool_call(tool,call['function']['arguments'],call_id=frame['id'],
                call_index=frame['index'],batch_index=batch_index,options=managed.options)
        except BaseException:managed.close();raise
        from .tool_dispatch import PreparedToolCallV1
        if not isinstance(result,PreparedToolCallV1):managed.finish(result.outcome)
        return result

    def start(self,prepared):
        from .tool_dispatch import start_prepared_tool
        start_prepared_tool(prepared)

    def settle(self,prepared,settlement):
        from .tool_dispatch import settle_custom_tool_call
        managed=self.frames[prepared.batch_index]['manager']
        try:
            result=settle_custom_tool_call(prepared,settlement)
            managed.finish(result.outcome)
            return result
        finally:managed.close()

    def serial(self,call,*,permit,batch_index,cancelled):
        from .tool_dispatch import WorkerSettlementV1
        from .events import emit
        frame,options=self._frame(call,permit,batch_index,cancelled)
        owner_cancelled=frame['owner_cancelled']
        options['cancelled']=lambda:cancelled() or owner_cancelled()
        options.pop('on_outcome',None)
        options.pop('check_boundary',None)
        clone=BoundedToolRegistry(self.registry.builtin,self.custom,self.registry.allowed,self.registry.check_boundary,
            execution_options=lambda:dict(options),index_all_calls=True,bounded_recovery=self.registry.bounded_recovery,
            terminal_capture=self.registry.terminal_capture,terminal_tools=self.registry.terminal_tools)
        clone.turn=self.registry.turn
        original=clone._outcome
        def report(outcome):original(outcome);frame['outcome']=outcome
        clone._outcome=report
        name=call['function']['name']
        from .tool_event_contract import managed_events
        if frame['refusal'] is None and not managed_events(self.custom.get(name)):
            if name in self.custom:
                options['_before_handler']=lambda arguments:emit('tool_started',name=name,call_id=call['id'],turn=self.registry.turn)
            else:emit('tool_started',name=name,call_id=call['id'],turn=self.registry.turn)
        try:
            message=(clone.refuse_message(call,frame['refusal']) if frame['refusal'] is not None else clone.execute_message(call))
            frame['outcome']=clone.last_outcome;frame['state']['code']=clone.last_outcome.code
            payload=json.loads(message['content'])
            return WorkerSettlementV1(frame['id'],frame['index'],batch_index,clone.last_outcome.code,
                json.dumps(payload,allow_nan=False).encode(),handler_entered=clone.last_outcome.to_dict()['effect']!='not_started' and not clone.last_outcome.replayed,
                replayed=clone.last_outcome.replayed)
        except BaseException:
            frame['outcome']=clone.last_outcome;frame['state']['code']=clone.last_outcome.code
            raise

    def restore(self,call,settlement):
        frame=self.frames[settlement.batch_index]
        self.registry.last_outcome=frame['outcome']
        self.registry.last_contract_call_id=frame['id']
        self.registry.last_contract_call_index=frame['index']

    def release(self,index):
        frame=self.frames.pop(index,None);self.options.pop(index,None)
        if frame is not None:
            manager=frame.get('manager')
            if manager is not None:manager.release()
            frame.clear()

    def release_all(self):
        for index in tuple(self.frames):self.release(index)

    def message(self,call,settlement):
        return dict(role='tool',tool_call_id=call['id'],content=json.dumps(settlement.outcome,allow_nan=False))


class ParallelBatchCursor:
    """Keep the established per-call reducer while advancing bounded windows."""
    def __init__(self,registry,backend,hooks,calls):
        from .execution_budget import reserve_tool_batch
        from .tool_parallel import ParallelToolCoordinator
        from .rule_hooks import STOP
        from .events import emit_after_completion
        self.dispatch=ParallelRegistryDispatch(registry,backend,hooks)
        self.reservation=reserve_tool_batch(backend.budget,backend.execution_budgets,len(calls))
        self.coordinator=ParallelToolCoordinator(backend.parallel_tools,check_boundary=backend.check_boundary,
            reservation=self.reservation,dispatch=self.dispatch,reduce=lambda c,s:s.outcome,emit=emit_after_completion)
        self.coordinator.plan(tuple(calls))
        self.iterator=self.coordinator.iter_results(tuple(calls))
        backend.budget.parallel_batches.append(self.coordinator.evidence)
    def execute_message(self,call):
        actual,settlement=next(self.iterator)
        if actual!=call:raise ValueError('parallel call order mismatch')
        return self.dispatch.message(actual,settlement)
    def __enter__(self):return self
    def __exit__(self,*exc):
        try:self.iterator.close()
        finally:self.coordinator.close()
