"""Released Harness runs TOON profiles unchanged: TOON on the wire, JSON in everything it keeps.

Harness 0.9.12 cannot pass tool-contract bindings to a step (it accepts only an unmodified ProsaicRuntime or a
RuntimeAdapter, and neither adds invocation options), so evidence delivery and citations are covered through
Runtime in test_toon_evidence.py, which test_toon_installed.py reruns from the installed wheel.
"""
from dataclasses import replace
import json
import sqlite3
from uuid import uuid4

import pytest
from toon_format import decode

from prosaic_runtime import ProsaicArtifact
from prosaic_harness import FileRunStore, Harness, WorkflowBindings, WorkflowCatalog, WorkflowFactory, WorkflowPolicy
from support.conversation_server import ConversationFixture
from support.tool_journal import SqliteJournal
from test_toon_tool_results import ROWS, tool_contents

TABLE = 'rows[2]{id,name}:'  # how TOON writes ROWS; never expected in anything persisted


def workflow(fx, journal):
    agent = ProsaicArtifact('approved', 'subagent', {'name': 'approved', 'description': 'Synthetic lookup',
                            'model_tier': 'fast', 'tools': ['lookup']}, 'Look the rows up, then return a JSON object.')
    config = replace(fx.runtime.config, routes={'fast': 'local'})
    return WorkflowFactory(
        catalog=WorkflowCatalog(agents={'author': agent},
                                schemas={'result': {'type': 'object', 'additionalProperties': True}}),
        bindings=WorkflowBindings(config=config, custom_tools=fx.tools, operation_namespace='toon-ops',
                                  tool_journal=journal),
        policy=WorkflowPolicy(allowed_agents={'author'}, allowed_schemas={'result'}, allowed_tools={'lookup'},
                              allowed_model_tiers={'fast'}, allow_agent_composition=True, max_calls=2,
                              max_attempts=1, max_provider_requests_per_invocation=2,
                              max_tool_calls_per_invocation=1)).build({
        'version': 1, 'name': 'toon-lookup', 'start': 'first', 'steps': {
            'first': {'kind': 'agent', 'agent': 'author', 'schema': 'result', 'tools': ['lookup'], 'next': 'approval'},
            'approval': {'kind': 'pause', 'question': 'Continue?', 'choices': {'approve': 'second'}},
            'second': {'kind': 'agent', 'agent': 'author', 'schema': 'result', 'tools': ['lookup'], 'next': 'done'},
            'done': {'kind': 'finish'}}})


def queue_lookup_step(fx):
    fx.queue_calls([{'id': 'call_1', 'name': 'lookup', 'arguments': '{}'}])
    fx.queue_text('{}')


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_tool_step_pause_and_resume_send_toon_and_keep_json(tmp_path, provider):
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': 'toon'}
        handled, saved = [], []

        def lookup(arguments, context):
            handled.append(1)
            return ROWS

        fx.add_tool('lookup', lookup, operation_key=lambda arguments, context: 'rows-1')
        journal = SqliteJournal(tmp_path / 'effects.sqlite')
        flow, run_id = workflow(fx, journal), uuid4().hex

        class LostAck(BaseException):
            pass

        class FaultStore(FileRunStore):
            def save_receipt(self, run_id, invocation_id, receipt, lease):
                super().save_receipt(run_id, invocation_id, receipt, lease)
                saved.append(receipt['id'])
                if receipt['step'] == 'second':
                    raise LostAck()

        def reopened(kind):
            store = kind(tmp_path / 'store', namespace='toon')
            store.initialize()
            return store

        store = reopened(FaultStore)
        try:
            # Ordinary tool execution: Harness builds its own Runtime from the admitted configuration.
            queue_lookup_step(fx)
            assert Harness(flow, store=store, run_id=run_id).run({})['status'] == 'waiting'
            [content] = tool_contents(fx.requests[1])
            assert TABLE in content and decode(content) == {'status': 'ok', 'result': ROWS}
            assert handled == [1] and len(fx.requests) == 2

            # Durable resume in a reopened store: the journaled lookup is replayed, not run again.
            store.close()
            store = reopened(FaultStore)
            queue_lookup_step(fx)
            paused = Harness(flow, store=store, run_id=run_id)
            with pytest.raises(LostAck):
                paused.resume(choice='approve', expected_revision=paused.status().revision)
            [content] = tool_contents(fx.requests[3])
            assert TABLE in content and decode(content) == {'status': 'ok', 'result': ROWS}
            assert handled == [1] and len(fx.requests) == 4

            # The receipt was saved before the lost acknowledgement: nothing completed is repeated.
            store.close()
            store = reopened(FileRunStore)
            fresh = Harness(flow, store=store, run_id=run_id)
            assert fresh.recovery_evidence()['recoveryStatus'] == 'receiptAvailable'
            assert fresh.resume()['status'] == 'completed'
            assert handled == [1] and len(fx.requests) == 4 and len(saved) == 2

            # Everything kept is JSON: the journaled outcome and both receipts.
            with sqlite3.connect(journal.path) as connection:
                [(outcome,)] = connection.execute('SELECT outcome FROM effects').fetchall()
            assert json.loads(outcome) == {'status': 'ok', 'result': ROWS}
            for receipt_id in saved:
                receipt = store.load_receipt(run_id, receipt_id)
                assert receipt['result']['exit_code'] == 0 and TABLE not in json.dumps(receipt)
        finally:
            store.close()

