"""Pending batch slots are atomically protected without inventing dispatches."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from prosaic_runtime import ExecutionBudget, RunPolicy
from prosaic_runtime.budgets import InvocationBudget, InvocationBudgetExceeded
from prosaic_runtime.execution_budget import reserve_tool_batch, reserve_dispatch


def invocation(cap): return InvocationBudget(RunPolicy(max_provider_requests=3,max_tool_calls=cap))
def parent(cap): return ExecutionBudget(timeout_s=30,max_provider_requests=3,max_tool_calls=cap)


def test_two_call_parallel_batch_with_cap_one_dispatches_nothing():
    inv=invocation(1); enclosing=parent(2)
    with pytest.raises(InvocationBudgetExceeded): reserve_tool_batch(inv,(enclosing,),2)
    assert inv.tool_calls==enclosing.tool_calls==0
    assert inv._pending_tool_calls==enclosing._pending_tool_calls==0


def test_default_sequential_batch_still_executes_first_call_with_cap_one():
    inv=invocation(1); inv.before_tool()
    with pytest.raises(InvocationBudgetExceeded): inv.before_tool()
    assert inv.tool_calls==1


def test_parent_child_reservations_commit_atomically():
    inv=invocation(3); outer=parent(3); child=parent(1)
    with pytest.raises(InvocationBudgetExceeded): reserve_tool_batch(inv,(outer,child),2)
    assert all(b.tool_calls==b._pending_tool_calls==0 for b in (inv,outer,child))


def test_competing_invocations_cannot_steal_pending_slots():
    outer=parent(1); barrier=Barrier(2)
    def contend():
        inv=invocation(1); barrier.wait()
        try: return reserve_tool_batch(inv,(outer,),1)
        except InvocationBudgetExceeded:return None
    with ThreadPoolExecutor(2) as pool: reservations=list(pool.map(lambda _:contend(),range(2)))
    winners=[r for r in reservations if r is not None]; assert len(winners)==1
    assert outer.tool_calls==0 and outer._pending_tool_calls==1
    with pytest.raises(InvocationBudgetExceeded):reserve_dispatch((outer,),tool=True)
    winners[0].release_unstarted()
    reserve_dispatch((outer,),tool=True); assert outer.tool_calls==1


def test_cancel_releases_only_unstarted_slots():
    inv=invocation(2); outer=parent(2); reservation=reserve_tool_batch(inv,(outer,),2)
    assert inv.tool_calls==outer.tool_calls==0 and inv._pending_tool_calls==2
    permit=reservation.start(0)
    assert permit.call_index==0 and inv.tool_calls==outer.tool_calls==1
    reservation.release_unstarted(); reservation.release_unstarted()
    assert inv._pending_tool_calls==outer._pending_tool_calls==0
    assert inv.tool_calls==outer.tool_calls==1
    with pytest.raises(ValueError): reservation.start(1)
    with pytest.raises(ValueError): reservation.start(0)


@pytest.mark.parametrize('classification',['denied','replayed'])
def test_refused_and_replayed_calls_are_counted_once(classification):
    inv=invocation(1); outer=parent(1); reservation=reserve_tool_batch(inv,(outer,),1)
    reservation.start(0)
    with pytest.raises(ValueError):reservation.start(0)
    reservation.release_unstarted()
    assert inv.tool_calls==outer.tool_calls==1


def test_pending_slots_protect_same_invocation_from_ordinary_dispatch():
    inv=invocation(2); reservation=reserve_tool_batch(inv,(),2)
    with pytest.raises(InvocationBudgetExceeded): inv.before_tool()
    reservation.release_unstarted(); inv.before_tool(); assert inv.tool_calls==1


def test_shared_usage_unknown_denies_batch_without_reserving():
    inv=invocation(1); outer=ExecutionBudget(timeout_s=30,max_provider_requests=1,max_tool_calls=1,max_reported_tokens=20)
    outer._usage_complete=False
    with pytest.raises(InvocationBudgetExceeded,match='usage_unknown'):reserve_tool_batch(inv,(outer,),1)
    assert inv._pending_tool_calls==outer._pending_tool_calls==0


def test_permits_are_scoped_single_use_and_cannot_be_forged():
    from prosaic_runtime.execution_budget import ToolAttemptPermitV1
    inv=invocation(2); reservation=reserve_tool_batch(inv,(),2)
    permit=reservation.start(0)
    with pytest.raises(ValueError): ToolAttemptPermitV1(reservation,0).consume(inv)
    with pytest.raises(ValueError): permit.consume(invocation(2))
    permit.consume(inv)
    with pytest.raises(ValueError): permit.consume(inv)
    reservation.release_unstarted()
