"""Invocation-local handler concurrency with a single state-owning coordinator."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
import json
from queue import Queue, Empty
from threading import Event, Lock
import time

from .conversation import encode_owned
from .events import Cancelled, worker_event_context, text_withheld
from .tool_dispatch import PreparedToolCallV1, WorkerSettlementV1, invoke_prepared_tool
from .tool_effects import ToolEffectFailure
from .tool_outcomes import ToolOutcome
from .tool_parallel_contracts import ParallelToolPolicyV1
from .tools import ToolDeadlineExceeded

_worker = ContextVar('prosaic_parallel_tool_worker', default=False)


def assert_not_parallel_worker():
    if _worker.get(): raise ValueError('parallel tool Runtime reentry is unsupported')


@dataclass(frozen=True)
class ParallelBatchResultV1:
    messages: tuple
    stopped: bool
    failure_code: str | None
    evidence: dict


@dataclass
class _Slot:
    index: int
    wire_call: dict
    prepared: PreparedToolCallV1
    ready: Event = field(default_factory=Event)
    permit: Event = field(default_factory=Event)
    allowed: bool = False
    future: object = None
    settlement: WorkerSettlementV1 | None = None


class ParallelToolCoordinator:
    def __init__(self, policy, *, check_boundary, reservation, dispatch, reduce, emit):
        if type(policy) is not ParallelToolPolicyV1: raise ValueError('invalid parallel tool policy')
        policy.__post_init__()
        self.policy=policy;self.check_boundary=check_boundary;self.reservation=reservation
        self.dispatch=dispatch;self.reduce=reduce;self.emit=emit
        self.cancelled=Event();self._progress=Queue(maxsize=policy.max_pending_events);self._progress_bytes=0
        self._progress_lock=Lock();self._event_failure=None;self._event_recorded=False
        self._errors=[];self._boundary_failed=False;self._pending={};self._inflight={};self._results={}
        self._pool=None;self._closed=False;self._withheld=text_withheld();self._calls=();self._sink_failed=False
        self.evidence=dict(version=1,batchId=dispatch.batch_id,calls=[])

    def _identity(self,call,index): return self.dispatch.identity(call,index)

    def _failure(self, error, index):
        self._errors.append((index,error));self.cancelled.set()

    def _tick(self):
        if self._event_failure is not None and not self._event_recorded:
            self._event_recorded=True;self._failure(self._event_failure,-1)
        if not self._boundary_failed:
            try:self.check_boundary()
            except BaseException as exc:self._boundary_failed=True;self._failure(exc,-1)

    def _enqueue(self,event):
        try:encoded=encode_owned(event,max_bytes=self.policy.max_pending_event_bytes,max_nodes=4096)
        except (TypeError,ValueError,UnicodeError):encoded=None
        with self._progress_lock:
            if self._event_failure is not None:return
            if (encoded is None or self._progress.full() or
                    self._progress_bytes+len(encoded)>self.policy.max_pending_event_bytes):
                self._event_failure=RuntimeError('parallel event capacity');self.cancelled.set();return
            self._progress.put_nowait(encoded);self._progress_bytes+=len(encoded)

    def _deliver_progress(self):
        while self._event_failure is None and not self._sink_failed:
            with self._progress_lock:
                try:encoded=self._progress.get_nowait()
                except Empty:return
                self._progress_bytes-=len(encoded)
            event=json.loads(encoded);kind=event.pop('event')
            try:self.emit(kind,**event)
            except BaseException as exc:self._sink_failed=True;self._failure(exc,-1);return

    def _run(self,slot):
        slot.ready.set();slot.permit.wait()
        if not slot.allowed:
            return WorkerSettlementV1(slot.prepared.call_id,slot.prepared.call_index,slot.index,'unclassified')
        token=_worker.set(True)
        try:
            with worker_event_context(self._enqueue,self.cancelled.is_set,withheld=self._withheld):
                return invoke_prepared_tool(slot.prepared)
        finally:_worker.reset(token)

    def _state(self,index,settlement,prepared=None):
        phase=(prepared.options.get('_phase_state',{}) if prepared is not None else
            getattr(self.dispatch,'options',{}).get(index,{}).get('_phase_state',{}))
        journal='notUsed';state='notStarted';code=phase.get('code',settlement.code if settlement is not None else None)
        entered=settlement.handler_entered if settlement is not None else bool(phase.get('handlerEntered',False))
        replayed=settlement.replayed if settlement is not None else False
        if replayed:state='replayed';journal='replayed'
        elif phase.get('status')=='completed':state='completed';journal='committed' if (prepared and prepared.operation_key) or phase.get('operation_key') is not None else 'notUsed'
        elif phase.get('status')=='uncertain' or prepared and prepared.operation_key or entered:
            state='uncertain';journal='claimedUncertain' if prepared and prepared.operation_key else 'notUsed'
        elif settlement is not None and code in {'ok','controlled_failure'}:state='completed'
        if prepared is None and phase.get('operation_key') is not None and state=='uncertain':journal='claimedUncertain'
        if code in {'not_granted','authorization_denied','invalid_arguments'} and not entered:state='notStarted'
        elif code not in {'ok','controlled_failure'} and entered:state='uncertain'
        start=settlement.started_at if settlement else None;finish=settlement.finished_at if settlement else None
        ident,call_index=self._identity(self._calls[index],index)
        return dict(callId=ident,callIndex=call_index,batchIndex=index,state=state,handlerEntered=entered,
            journalState=journal,outcomeCode=code,durationMs=round(max(0,finish-start)*1000,3) if start is not None and finish is not None else 0)

    def _record(self,index,settlement,prepared=None):
        record=self._state(index,settlement,prepared)
        self.evidence['calls'][index]=record
        if not self._sink_failed:
            try:self.emit('tool_execution_settled',parallelToolV1=dict(version=1,batchId=self.dispatch.batch_id,**record))
            except BaseException as exc:self._sink_failed=True;self._failure(exc,index)

    def _settle(self,slot):
        try:settlement=slot.future.result()
        except BaseException as exc:
            settlement=WorkerSettlementV1(slot.prepared.call_id,slot.prepared.call_index,slot.index,'unclassified',
                handler_entered=slot.allowed,exception=exc)
        slot.settlement=settlement
        if not slot.allowed and slot.prepared.operation_key is not None:
            settlement=WorkerSettlementV1(slot.prepared.call_id,slot.prepared.call_index,slot.index,'tool_effect_uncertain',
                exception=ToolEffectFailure('tool_effect_uncertain'))
        try:settlement=self.dispatch.settle(slot.prepared,settlement)
        except BaseException as exc:self._failure(exc,slot.index)
        else:
            outcome=ToolOutcome(settlement.code,settlement.replayed).to_dict()
            if outcome['effect']=='unknown':
                self._failure(ToolEffectFailure('tool_effect_uncertain'),slot.index)
            elif getattr(self.dispatch,'bounded_recovery',False) and outcome['recovery'] in {'stop','reconcile'}:
                self._failure(ToolEffectFailure('tool_recovery_blocked'),slot.index)
        self._results[slot.index]=settlement
        self._record(slot.index,settlement,slot.prepared)
        if slot.prepared.operation_key is not None:
            self._inflight.pop((slot.prepared.context.scope.operation_namespace,slot.prepared.operation_key),None)

    def _drain(self):
        while self._pending:
            self._tick()
            done,_=wait(tuple(self._pending),timeout=.05,return_when=FIRST_COMPLETED)
            for future in sorted(done,key=lambda f:self._pending[f].index):
                slot=self._pending.pop(future);self._settle(slot)
            self._deliver_progress()
        self._tick();self._deliver_progress()

    def _await_operation(self,namespace,key,signature):
        slot=self._inflight.get((namespace,key))
        if slot is None:return False
        if slot.prepared.signature!=signature:raise ToolEffectFailure('tool_identity_conflict')
        self._drain()
        if self._errors:self._raise_failure()
        return True

    def _raise_failure(self):
        def rank(item):
            index,error=item
            priority=(3 if error.reason=='tool_recovery_blocked' else 0) if isinstance(error,ToolEffectFailure) else 2 if isinstance(error,(Cancelled,ToolDeadlineExceeded)) else 1
            return priority,index
        raise min(self._errors,key=rank)[1]

    def _eligible(self,call):
        registered=self.dispatch.custom.get(call['function']['name'])
        return registered is not None and registered.parallel_read

    def _windows(self,calls):
        index=0
        while index<len(calls):
            if not self._eligible(calls[index]):yield (index,);index+=1;continue
            end=index
            while end<len(calls) and end-index<self.policy.max_concurrency and self._eligible(calls[end]):end+=1
            yield tuple(range(index,end));index=end

    def _window(self,indices):
        for index in indices:
            self._tick()
            if self.cancelled.is_set():break
            call=self._calls[index];permit=self.reservation.start(index)
            try:
                prepared=self.dispatch.prepare(call,permit=permit,batch_index=index,
                    cancelled=self.cancelled.is_set,await_operation=self._await_operation)
                if not isinstance(prepared,PreparedToolCallV1):
                    self._results[index]=prepared;self._record(index,prepared)
                    if (getattr(self.dispatch,'bounded_recovery',False) and not getattr(self.dispatch,'is_rule_refusal',lambda i:False)(index)
                            and ToolOutcome(prepared.code,prepared.replayed).to_dict()['recovery'] in {'stop','reconcile'}):
                        raise ToolEffectFailure('tool_recovery_blocked')
                    continue
                slot=_Slot(index,call,prepared)
                if prepared.operation_key is not None:self._inflight[(prepared.context.scope.operation_namespace,prepared.operation_key)]=slot
                if self._pool is None:self._pool=ThreadPoolExecutor(max_workers=self.policy.max_concurrency,thread_name_prefix='prosaic-read')
                slot.future=self._pool.submit(Context().run,self._run,slot);self._pending[slot.future]=slot
                while not slot.ready.wait(.05):
                    self._tick()
                    if self.cancelled.is_set():break
                self._tick()
                if not self.cancelled.is_set():
                    try:self.dispatch.start(prepared)
                    except BaseException as exc:self._sink_failed=True;self._failure(exc,index)
                    else:
                        slot.allowed=True
                        # Start delivery commits entry, even if this boundary now stops later calls.
                        self._tick()
                slot.permit.set()
            except BaseException as exc:
                self._failure(exc,index)
                if index not in self._results and not any(s.index==index for s in self._pending.values()):self._record(index,None)
        self._drain()
        if self._errors:self._raise_failure()

    def plan(self,calls):
        if self._calls:raise ValueError('parallel coordinator already planned')
        self._calls=tuple(calls)
        for index,call in enumerate(calls):
            ident,call_index=self._identity(call,index)
            self.evidence['calls'].append(dict(callId=ident,callIndex=call_index,batchIndex=index,state='notStarted',
                handlerEntered=False,journalState='notUsed',outcomeCode=None,durationMs=0))
    def iter_results(self,calls):
        if not self._calls:self.plan(calls)
        elif tuple(calls)!=self._calls:raise ValueError('parallel coordinator call drift')
        try:
            for indices in self._windows(calls):
                if self._eligible(calls[indices[0]]) and sum(self.dispatch.custom[calls[i]['function']['name']].max_result_bytes+1536 for i in indices)>self.policy.max_buffered_result_bytes:
                    raise ValueError('parallel result capacity')
            for indices in self._windows(calls):
                self._tick()
                if self._errors:self._raise_failure()
                if self._eligible(calls[indices[0]]):self._window(indices)
                else:
                    index=indices[0];permit=self.reservation.start(index)
                    try:settlement=self.dispatch.serial(calls[index],permit=permit,batch_index=index,cancelled=self.cancelled.is_set)
                    except BaseException:
                        self._record(index,None)
                        raise
                    self._results[index]=settlement;self._record(index,settlement)
                    if ToolOutcome(settlement.code,settlement.replayed).to_dict()['effect']=='unknown':
                        self._failure(ToolEffectFailure('tool_effect_uncertain'),index)
                    del settlement
                    if self._errors:self._raise_failure()
                for index in indices:
                    result=self._results.pop(index);self.dispatch.restore(calls[index],result)
                    try:yield calls[index],result
                    finally:
                        release=getattr(self.dispatch,'release',None)
                        if release is not None:release(index)
                        del result
        finally:self.close()

    def execute(self,calls):
        from .rule_hooks import STOP
        messages=[];stopped=False
        iterator=self.iter_results(calls)
        try:
            for call,settlement in iterator:
                message=self.reduce(call,settlement)
                if message is STOP:stopped=True;break
                messages.append(message)
                del settlement
        finally:iterator.close()
        return ParallelBatchResultV1(tuple(messages),stopped,None,self.evidence)

    def close(self):
        if self._closed:return
        try:
            for slot in self._pending.values():
                if not slot.permit.is_set():slot.permit.set()
            self._drain()
        finally:
            if self._pool is not None:self._pool.shutdown(wait=True,cancel_futures=True)
            self._results.clear()
            release_all=getattr(self.dispatch,'release_all',None)
            if release_all is not None:release_all()
            self.reservation.release_unstarted();self._closed=True
