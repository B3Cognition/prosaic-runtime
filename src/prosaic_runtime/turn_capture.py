"""Invocation-local neutral turn capture; provider continuations stay private."""
import json

from .conversation import CompletedTurn, TurnEvidence, TurnResult, encode_owned
from .types import Result


def failure_code(result):
    if result.exit_code == 0:
        return None
    reason = result.metadata.get('failure_reason') or result.metadata.get('provider_error_code')
    return {'cancelled': 'cancelled', 'invocation_timeout': 'timeout',
        'invalidToolRequest': 'invalidToolRequest', 'contextLimit': 'contextLimit',
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

    def assistant(self, turn):
        self.exchange += 1
        self.call_ids = {call['id']: f'{self.scope.invocation_id}:{self.exchange}:{index}'
                         for index, call in enumerate(turn.tool_calls)}
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

    def tool(self, call, message):
        outcome = json.loads(message['content'])
        call_id = self.call_ids[call['id']]
        self.messages.append({'role': 'tool', 'content': [
            {'type': 'toolOutcome', 'callId': call_id, 'outcome': outcome}]})
        self.tool_outcomes.append({'callId': call_id, 'name': call['function']['name'],
                                  'status': 'completed', 'outcome': outcome})

    def finish(self, result, budget):
        code = failure_code(result)
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
        metadata = {k: v for k, v in result.metadata.items() if k in {
            'provider', 'finish_reason', 'streamed', 'token_usage_details', 'invocation_budgets_v1'}}
        if code is not None:
            metadata['failure_reason'] = code
        attribution = result.metadata.get('accounting_v1')
        if type(attribution) is dict:
            metadata['accounting_v1'] = {k: v for k, v in attribution.items()
                                       if k in {'context', 'namespace', 'environment'}}
        try:
            metadata = json.loads(encode_owned(metadata, max_bytes=self.limits.max_metadata_bytes,
                max_depth=self.limits.max_json_depth, max_nodes=self.limits.max_json_nodes))
        except (ValueError, TypeError, UnicodeError):
            metadata = {'failure_reason': code} if code else {}
        backend = self.backend
        known = {}
        if backend is not None:
            details = backend.reported_usage_details
            for original, public in [('prompt_tokens', 'inputTokens'),
                                      ('completion_tokens', 'outputTokens'), ('total_tokens', 'totalTokens')]:
                if type(details.get(original)) is int and details[original] >= 0:
                    known[public] = details[original]
        evidence = TurnEvidence.from_dict({'version': 1,
            'providerAttempts': budget.provider_requests, 'toolAttempts': budget.tool_calls,
            'knownUsage': known, 'usageComplete': backend.usage_complete if backend else budget.provider_requests == 0,
            'toolOutcomes': self.tool_outcomes, 'failureCode': code,
            'accounting': attribution if type(attribution) is dict else None})
        sanitized = Result(result.exit_code if code != 'outputLimit' else 1,
            result.stdout if code is None else '', code or '', token_usage=result.token_usage,
            timed_out=result.timed_out, metadata=metadata)
        return TurnResult(sanitized, completed, evidence)
