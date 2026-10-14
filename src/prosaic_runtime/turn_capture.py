"""Invocation-local neutral turn capture; provider continuations stay private."""
import json
import hashlib
from dataclasses import replace

from .conversation import CompletedTurn, TurnEvidence, TurnResult, encode_owned
from .types import Result
from .http_bounds import LimitExceeded


class TurnOutputExceeded(LimitExceeded):
    def __init__(self):
        super().__init__('outputLimit')


def failure_code(result):
    if result.exit_code == 0:
        return None
    reason = result.metadata.get('failure_reason') or result.metadata.get('provider_error_code')
    return {'cancelled': 'cancelled', 'invocation_timeout': 'timeout',
        'invalidToolRequest': 'invalidToolRequest', 'contextLimit': 'contextLimit',
        'bindingMismatch': 'bindingMismatch',
        'budget_exceeded': 'budgetExceeded', 'provider_request_limit': 'budgetExceeded',
        'tool_call_limit': 'budgetExceeded', 'token_limit': 'budgetExceeded', 'usage_unknown': 'budgetExceeded',
        'accounting_failed': 'accountingFailure', 'critical_hook_error': 'observationFailure',
        'tool_effect_uncertain': 'toolEffectUncertain', 'tool_journal_failed': 'toolJournalFailure',
        'tool_identity_conflict': 'toolEffectUncertain', 'incomplete_generation': 'incompleteGeneration',
        'incomplete_response': 'incompleteGeneration', 'outputLimit': 'outputLimit'}.get(
            reason, 'timeout' if result.timed_out else 'providerFailure')


class TurnCapture:
    def __init__(self, message, scope, limits):
        self.scope, self.limits = scope, limits
        self.messages = [{'role': 'user', 'content': [{'type': 'text', 'text': message}],
                          'turnStatus': 'completed'}]
        self.exchange = 0
        self.call_ids = {}
        self.tool_outcomes = []
        self.backend = None
        self.tool_indices = {}
        self.provider_observations = {}
        self.accounting = None
        self.evidence_overflow = False

    def bind_accounting(self, context, recorder, provider_limit, tool_limit):
        self.accounting = {'context': context.to_dict(), 'namespace': getattr(recorder, 'namespace', None),
                           'environment': getattr(recorder, 'environment', None), 'provider_call_ids': []}
        try:
            size = len(encode_owned(self.accounting))
            if size + 1024 + provider_limit * 80 + tool_limit * 1536 > self.limits.max_evidence_bytes:
                from .conversation import TurnAdmissionError
                raise TurnAdmissionError('invalidPolicy')
        except (ValueError, TypeError, UnicodeError) as exc:
            from .conversation import TurnAdmissionError
            raise TurnAdmissionError('invalidPolicy') from exc

    def record_provider_observation(self, call_id, observation):
        usage = observation['usage']
        self.provider_observations[call_id] = {k: usage.get(k) for k in
                                              ('input_tokens', 'output_tokens', 'total_tokens', 'status')}
        if self.accounting is not None and call_id not in self.accounting['provider_call_ids']:
            self.accounting['provider_call_ids'].append(call_id)

    def _check_turn_size(self):
        try:
            encode_owned({'version': 1, 'messages': self.messages}, max_bytes=self.limits.max_turn_bytes,
                         max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes)
        except (ValueError, TypeError, UnicodeError):
            raise TurnOutputExceeded() from None
        if self.evidence_overflow:
            raise TurnOutputExceeded()

    def event(self, event, sink):
        kind = event.get('event')
        if kind not in {'started', 'completed', 'text', 'text_delta', 'tool_started', 'tool_completed'}:
            return
        owned = {'event': kind, 'provisional': True}
        if kind in {'text', 'text_delta'}:
            owned['text'] = event.get('text', '')
        elif kind in {'tool_started', 'tool_completed'}:
            owned['name'] = event.get('name')
            owned['call_id'] = self.call_ids.get(event.get('call_id'))
            if kind == 'tool_completed':
                owned['status'] = event.get('status')
        elif kind == 'completed':
            owned.update({k: event[k] for k in ('exit_code', 'token_usage') if k in event})
        try:
            owned = json.loads(encode_owned(owned, max_bytes=self.limits.max_event_bytes,
                max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes))
        except (ValueError, TypeError, UnicodeError):
            raise TurnOutputExceeded() from None
        if sink is not None:
            sink(owned)

    def assistant(self, turn):
        self.exchange += 1
        self.call_ids = {call['id']: hashlib.sha256(
                            f'{self.scope.invocation_id}:{self.exchange}:{index}'.encode()).hexdigest()
                         for index, call in enumerate(turn.tool_calls)}
        first = len(self.tool_indices)
        self.tool_indices.update({first + i: {'callId': self.call_ids[call['id']],
                                             'name': call['function']['name']}
                                  for i, call in enumerate(turn.tool_calls)})
        blocks = []
        if turn.continuation is not None:
            for block in turn.continuation:
                if block['type'] == 'text':
                    blocks.append({'type': 'text', 'text': block['text']})
                else:
                    blocks.append({'type': 'toolCall', 'callId': self.call_ids[block['id']],
                                   'name': block['name'], 'arguments': block['input']})
        else:
            if turn.text or not turn.tool_calls:
                blocks.append({'type': 'text', 'text': turn.text})
            blocks.extend({'type': 'toolCall', 'callId': self.call_ids[call['id']],
                           'name': call['function']['name'],
                           'arguments': json.loads(call['function']['arguments'])} for call in turn.tool_calls)
        self.messages.append({'role': 'assistant', 'content': blocks})
        self._check_turn_size()

    def record_tool_completion(self, index, outcome, status='completed', *, operation_key=None, signature=None):
        record = {**self.tool_indices[index], 'callIndex': index, 'status': status}
        if signature is not None:
            record['signature'] = signature
        if operation_key is not None:
            record['operationNamespace'] = self.scope.operation_namespace
            record['operationKey'] = operation_key
            record['operationId'] = hashlib.sha256(
                json.dumps([self.scope.operation_namespace, operation_key, signature]).encode()).hexdigest()
        if outcome is not None:
            try:
                record['outcome'] = json.loads(encode_owned(outcome, max_bytes=self.limits.max_evidence_bytes,
                    max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes))
                encode_owned(self.tool_outcomes + [record], max_bytes=max(1, self.limits.max_evidence_bytes - 1024))
            except (ValueError, TypeError, UnicodeError):
                record.pop('outcome', None)
                self.evidence_overflow = True
        # Exactly one terminal status per attempted tool. Unknown acknowledgments
        # never upgrade to completion without authoritative journal evidence.
        self.tool_outcomes.append(record)

    def wire_turn(self, turn):
        calls = [{**call, 'id': self.call_ids[call['id']]} for call in turn.tool_calls]
        continuation = None if turn.continuation is None else [
            {**block, 'id': self.call_ids[block['id']]} if block['type'] == 'tool_use' else dict(block)
            for block in turn.continuation]
        return replace(turn, tool_calls=calls, continuation=continuation)

    def wire_tool(self, message):
        return {**message, 'tool_call_id': self.call_ids[message['tool_call_id']]}

    def tool(self, call, message):
        outcome = json.loads(message['content'])
        call_id = self.call_ids[call['id']]
        self.messages.append({'role': 'tool', 'content': [
            {'type': 'toolOutcome', 'callId': call_id, 'outcome': outcome}]})
        if not any(record['callId'] == call_id for record in self.tool_outcomes):
            index = next(i for i, record in self.tool_indices.items() if record['callId'] == call_id)
            self.record_tool_completion(index, outcome)
        self._check_turn_size()

    def usage(self, budget):
        known = {}
        complete = budget.provider_requests == 0
        if self.provider_observations:
            complete = len(self.provider_observations) == budget.provider_requests
            for usage in self.provider_observations.values():
                if usage['status'] != 'reported':
                    complete = False
                    continue
                for key, public in [('input_tokens', 'inputTokens'), ('output_tokens', 'outputTokens'),
                                    ('total_tokens', 'totalTokens')]:
                    if type(usage[key]) is int and usage[key] >= 0:
                        known[public] = known.get(public, 0) + usage[key]
        elif self.backend is not None:
            complete = self.backend.usage_complete and self.backend.turns >= budget.provider_requests
            for key, public in [('prompt_tokens', 'inputTokens'), ('completion_tokens', 'outputTokens'),
                                ('total_tokens', 'totalTokens')]:
                value = self.backend.known_usage_details.get(key)
                if type(value) is int and value >= 0:
                    known[public] = value
        return known, complete

    def finish(self, result, budget):
        code = failure_code(result)
        if self.evidence_overflow:
            code = code or 'outputLimit'
        completed = None
        if code is None:
            try:
                if len(result.stdout.encode('utf-8')) > self.limits.max_assistant_bytes:
                    raise ValueError('assistant bound')
                document = {'version': 1, 'messages': self.messages}
                encode_owned(document, max_bytes=self.limits.max_turn_bytes,
                             max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes)
                completed = CompletedTurn.from_dict(document)
            except (ValueError, TypeError, UnicodeError):
                code = 'outputLimit'
        known, complete = self.usage(budget)
        attribution = self.accounting or result.metadata.get('accounting_v1')
        document = {'version': 1, 'providerAttempts': budget.provider_requests, 'toolAttempts': budget.tool_calls,
                    'knownUsage': known, 'usageComplete': complete, 'toolOutcomes': self.tool_outcomes,
                    'failureCode': code, 'accounting': attribution if type(attribution) is dict else None}
        try:
            encode_owned(document, max_bytes=self.limits.max_evidence_bytes,
                         max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes)
        except (ValueError, TypeError, UnicodeError):
            code = code or 'outputLimit'
            completed = None
            document['failureCode'] = code
            document['toolOutcomes'] = [{k: v for k, v in record.items() if k != 'outcome'}
                                        for record in self.tool_outcomes]
            encode_owned(document, max_bytes=self.limits.max_evidence_bytes,
                         max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes)
        evidence = TurnEvidence.from_dict(document)
        metadata = {k: v for k, v in result.metadata.items() if k in {
            'provider', 'finish_reason', 'streamed', 'token_usage_details', 'invocation_budgets_v1'}}
        if (type(metadata.get('finish_reason')) is not str or metadata['finish_reason'] not in {
                'stop', 'tool_calls', 'length', 'content_filter', 'end_turn', 'tool_use',
                'max_tokens', 'stop_sequence', 'refusal', 'pause_turn'}):
            metadata.pop('finish_reason', None)
        metadata['invocation_budgets_v1'] = {'provider_requests': budget.provider_requests,
            'tool_calls': budget.tool_calls, 'reported_tokens': known.get('totalTokens', 0), 'usage_complete': complete}
        if code is not None:
            metadata['failure_reason'] = code
        if type(attribution) is dict:
            metadata['accounting_v1'] = {k: v for k, v in attribution.items()
                                       if k in {'context', 'namespace', 'environment'}}
        try:
            metadata = json.loads(encode_owned(metadata, max_bytes=self.limits.max_metadata_bytes,
                max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes))
        except (ValueError, TypeError, UnicodeError):
            metadata = {'failure_reason': code} if code else {}
        # Durable conversation receipts use unsigned exit statuses. The legacy
        # OpenAI-compatible transport reports socket/deadline timeouts as -1.
        exit_code = 124 if result.exit_code < 0 and result.timed_out else result.exit_code
        sanitized = Result(exit_code if code != 'outputLimit' else 1,
            result.stdout if code is None else '', code or '', token_usage=known.get('totalTokens') if complete else None,
            timed_out=result.timed_out, metadata=metadata)
        return TurnResult(sanitized, completed, evidence)
