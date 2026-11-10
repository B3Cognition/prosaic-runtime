from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from threading import Barrier, Event, Lock, get_ident
import pytest
from parallel_tool_helpers import tool,call,coordinator
from prosaic_runtime import ParallelToolPolicyV1
from prosaic_runtime.rule_hooks import STOP


def test_independent_reads_overlap_and_callbacks_stay_on_owner():
    rendezvous=Barrier(2);workers=[];events=[];owner=get_ident()
    def read(args,ctx):workers.append(get_ident());rendezvous.wait(timeout=3);return args
    coord,dispatch=coordinator({'lookup':tool('lookup',read)},[call(0),call(1)],event=lambda *a,**kw:events.append((get_ident(),a,kw)))
    result=coord.execute((call(0),call(1)))
    assert [m['result']['i'] for m in result.messages]==[0,1]
    assert len(set(workers))==2 and owner not in workers
    assert all(t==owner for t in dispatch.callback_threads) and all(e[0]==owner for e in events)
    assert len([e for e in events if e[1][0]=='tool_execution_settled'])==2


def test_concurrency_cap_and_window_reduce_precede_next_window():
    lock=Lock();active=peak=0;history=[];gate=Barrier(2)
    def read(args,ctx):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active);history.append(('start',args['i']))
        if args['i']<4:gate.wait(timeout=3)
        with lock:active-=1
        return args
    def reduce(c,s):history.append(('reduce',s.batch_index));return s.outcome
    calls=tuple(call(i) for i in range(5));coord,_=coordinator({'lookup':tool('lookup',read)},calls,reduce=reduce)
    assert len(coord.execute(calls).messages)==5 and peak==2
    assert history.index(('reduce',1))<history.index(('start',2))
    assert history.index(('reduce',3))<history.index(('start',4))


def test_serial_barriers_never_overlap_reads():
    history=[];owner=get_ident()
    reads=tool('lookup',lambda a,c:history.append((a['i'],get_ident())) or a)
    serial=tool('barrier',lambda a,c:history.append((a['i'],get_ident())) or a,parallel=False)
    calls=(call(0),call(1,'barrier'),call(2));coord,_=coordinator({'lookup':reads,'barrier':serial},calls)
    assert len(coord.execute(calls).messages)==3
    assert [x[0] for x in history]==[0,1,2] and history[1][1]==owner


@pytest.mark.parametrize('order',list(permutations(range(3))))
def test_finish_order_never_changes_result_reduction(order):
    entered=Barrier(3);release=[Event() for _ in range(3)];finished=[Event() for _ in range(3)]
    def read(args,ctx):
        i=args['i'];entered.wait(timeout=3);assert release[i].wait(3);finished[i].set();return args
    calls=tuple(call(i) for i in range(3));coord,_=coordinator({'lookup':tool('lookup',read)},calls,
        policy=ParallelToolPolicyV1(max_concurrency=3))
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(coord.execute,calls)
        for i in order:release[i].set();assert finished[i].wait(3)
        result=future.result(timeout=3)
    assert [m['result']['i'] for m in result.messages]==[0,1,2]


def test_guard_stop_prevents_next_window_and_retains_started_siblings():
    seen=[];calls=tuple(call(i) for i in range(4))
    coord,dispatch=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(a['i']) or a)},calls,reduce=lambda c,s:STOP)
    result=coord.execute(calls)
    assert result.stopped and sorted(seen)==[0,1] and sorted(i for i,_,_ in dispatch.completed)==[0,1]
    assert dispatch.budget.tool_calls==2 and dispatch.budget._pending_tool_calls==0


def test_declared_output_capacity_rejects_before_any_callback():
    seen=[];calls=(call(0),call(1));coord,dispatch=coordinator({'lookup':tool('lookup',lambda a,c:seen.append(1))},calls,
        policy=ParallelToolPolicyV1(max_buffered_result_bytes=100))
    with pytest.raises(ValueError,match='parallel result capacity'):coord.execute(calls)
    assert not seen and not dispatch.callback_threads and dispatch.budget.tool_calls==0
    assert dispatch.budget._pending_tool_calls==0
