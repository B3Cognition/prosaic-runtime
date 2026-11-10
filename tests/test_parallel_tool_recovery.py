from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from parallel_tool_helpers import tool,call,coordinator
from prosaic_runtime import ParallelToolPolicyV1,ToolClaim
from prosaic_runtime.events import Cancelled,emit
from prosaic_runtime.tool_effects import ToolEffectFailure


class Journal:
    def __init__(self,state='new'):self.state=state;self.commits=[];self.signature=None
    def claim(self,namespace,key,signature):
        self.signature=signature
        if self.state=='replay':return ToolClaim('replay',signature,outcome=self.commits[-1][3])
        return ToolClaim(self.state,signature,'token' if self.state=='new' else None)
    def commit(self,*args):self.commits.append(args);self.state='replay'


def test_same_key_waits_for_owned_completion_then_replays():
    journal=Journal();seen=[]
    registered=tool('lookup',lambda a,c:seen.append(1) or {'ok':True},operation_key=lambda a,c:'same')
    # Arguments must also match for a journal replay.
    calls=(call(0),{**call(0),'id':'other'})
    coord,dispatch=coordinator({'lookup':registered},calls,journal=journal)
    assert len(coord.execute(calls).messages)==2 and seen==[1]
    assert [s for _,_,s in dispatch.completed]==['completed','replayed']


def test_unowned_uncertain_claim_never_starts_handler():
    journal=Journal('uncertain');seen=[];calls=(call(0),call(1))
    coord,_=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(1),operation_key=lambda a,c:'same')},calls,journal=journal)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert seen==[] and journal.commits==[]


def test_cancellation_drains_started_callbacks_and_keeps_completion():
    started=[Event(),Event()];release=Event();cancel=Event();seen=[]
    def check():
        if cancel.is_set():raise Cancelled()
    def read(a,c):started[a['i']].set();assert release.wait(3);seen.append(a['i']);return a
    calls=tuple(call(i) for i in range(3));coord,dispatch=coordinator({'lookup':tool('lookup',read)},calls,boundary=check)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(coord.execute,calls)
        assert all(e.wait(3) for e in started);cancel.set();assert coord.cancelled.wait(3)
        assert not future.done();release.set()
        with pytest.raises(Cancelled):future.result(timeout=3)
    assert sorted(seen)==[0,1] and sorted(i for i,_,_ in dispatch.completed)==[0,1]
    assert dispatch.budget._pending_tool_calls==0


def test_progress_overflow_cannot_drop_known_settlement_or_deadlock_join():
    calls=(call(0),);seen=[]
    def read(a,c):
        try:
            for _ in range(10):emit('progress',text='x'*40)
        except Cancelled:pass
        return {'ok':True}
    coord,dispatch=coordinator({'lookup':tool('lookup',read)},calls,
        policy=ParallelToolPolicyV1(max_pending_events=1,max_pending_event_bytes=128))
    with pytest.raises(RuntimeError,match='parallel event capacity'):coord.execute(calls)
    assert dispatch.completed[0][1]=={'status':'ok','result':{'ok':True}}


def test_start_sink_failure_after_claim_keeps_uncertain_without_handler():
    journal=Journal();seen=[]
    def event(kind,**data):
        if kind=='tool_started':raise RuntimeError('start sink')
    calls=(call(0),);coord,dispatch=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(1),operation_key=lambda a,c:'key')},
        calls,journal=journal,event=event)
    with pytest.raises((RuntimeError,ToolEffectFailure)):coord.execute(calls)
    assert not seen and not journal.commits and dispatch.completed[0][2]=='uncertain'
    assert coord.evidence['calls'][0]['journalState']=='claimedUncertain'


def test_cancel_after_successful_start_commits_to_entering_handler_once():
    cancel=Event();seen=[]
    def boundary():
        if cancel.is_set():raise Cancelled()
    def event(kind,**data):
        if kind=='tool_started':cancel.set()
    calls=(call(0),);coord,_=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(c.cancelled()) or {'ok':True})},
        calls,boundary=boundary,event=event)
    with pytest.raises(Cancelled):coord.execute(calls)
    assert len(seen)==1


def test_same_key_different_signature_conflicts_without_second_handler():
    journal=Journal();seen=[];calls=(call(0),call(1))
    coord,_=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(a['i']) or a,operation_key=lambda a,c:'same')},calls,journal=journal)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert seen==[0] and len(journal.commits)==1


def test_fast_known_sibling_survives_slow_unknown_failure():
    fast=Event();calls=(call(0),call(1))
    def read(a,c):
        if a['i']==0:fast.set();return a
        assert fast.wait(3);raise RuntimeError('PRIVATE')
    coord,dispatch=coordinator({'lookup':tool('lookup',read)},calls)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert next(out for i,out,_ in dispatch.completed if i==0)=={'status':'ok','result':{'i':0}}
    assert coord.evidence['calls'][0]['state']=='completed'


def test_lost_commit_ack_does_not_claim_authoritative_completion():
    class LostAck(Journal):
        def commit(self,*args):super().commit(*args);raise RuntimeError('PRIVATE')
    journal=LostAck();calls=(call(0),)
    coord,dispatch=coordinator({'lookup':tool('lookup',lambda a,c:a,operation_key=lambda a,c:'key')},calls,journal=journal)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert len(journal.commits)==1 and dispatch.completed[0][2]=='uncertain'
    assert coord.evidence['calls'][0]['state']=='uncertain'


def test_worker_reentry_marker_refuses_execution_and_resets_after_join():
    from prosaic_runtime.tool_parallel import assert_not_parallel_worker
    calls=(call(0),);seen=[]
    def read(a,c):assert_not_parallel_worker();seen.append(1)
    coord,_=coordinator({'lookup':tool('lookup',read)},calls)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert seen==[];assert_not_parallel_worker()


@pytest.mark.parametrize('value',[object(),'x'*100000])
def test_unusable_result_stops_next_window(value):
    seen=[];calls=(call(0),call(1))
    coord,_=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(a['i']) or value)},calls,
        policy=ParallelToolPolicyV1(max_concurrency=1))
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert seen==[0]


def test_two_simultaneous_invocations_do_not_share_worker_event_context():
    events=[[],[]]
    def run(index):
        calls=(call(0),call(1));scope=str(index)
        def read(a,c):emit('progress',text=c.scope.invocation_id);return a
        coord,_=coordinator({'lookup':tool('lookup',read)},calls,scope=scope,
            event=lambda kind,**data:events[index].append((kind,data)))
        return coord.execute(calls)
    with ThreadPoolExecutor(2) as pool:assert all(len(r.messages)==2 for r in pool.map(run,(0,1)))
    for index,rows in enumerate(events):
        assert [data['text'] for kind,data in rows if kind=='progress']==[str(index),str(index)]


def test_progress_sink_is_not_called_again_after_critical_failure():
    calls=(call(0),);seen=[]
    def read(a,c):
        for i in range(3):emit('progress',text=str(i))
        return a
    def event(kind,**data):
        if kind=='progress':seen.append(1);raise RuntimeError('sink')
    coord,dispatch=coordinator({'lookup':tool('lookup',read)},calls,event=event)
    with pytest.raises(RuntimeError,match='sink'):coord.execute(calls)
    assert seen==[1] and dispatch.completed[0][2]=='completed'


def test_cancel_between_claim_and_entry_is_uncertain_and_not_reclaimed():
    cancelled=Event();seen=[]
    class CancelClaim(Journal):
        def claim(self,*args):result=super().claim(*args);cancelled.set();return result
    def boundary():
        if cancelled.is_set():raise Cancelled()
    calls=(call(0),);coord,dispatch=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(1),operation_key=lambda a,c:'key')},
        calls,journal=CancelClaim(),boundary=boundary)
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert seen==[] and dispatch.completed[0][2]=='uncertain'
    assert coord.evidence['calls'][0]['journalState']=='claimedUncertain'


def test_lost_commit_schedule_carries_journal_failure_not_worker_success():
    class LostAck(Journal):
        def commit(self,*args):super().commit(*args);raise RuntimeError('PRIVATE')
    calls=(call(0),);coord,_=coordinator({'lookup':tool('lookup',lambda a,c:a,operation_key=lambda a,c:'key')},calls,journal=LostAck())
    with pytest.raises(ToolEffectFailure):coord.execute(calls)
    assert coord.evidence['calls'][0]['outcomeCode']=='tool_journal_failed'
