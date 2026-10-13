"""Host-owned, process-local allowance across nested synchronous Runtime calls."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
from threading import RLock
import time

from .budgets import InvocationBudgetExceeded
from .events import Cancelled
from .tools import ToolDeadlineExceeded

_active = ContextVar('prosaic_execution_budgets', default=())


class ExecutionBudget:
    """A fixed deadline and dispatch caps; activation never replenishes allowance.

    This is admission, not a durable ledger or a sandbox for trusted callbacks.
    Activate the same object explicitly in worker threads to share its counters.
    """
    def __init__(self, *, timeout_s, max_provider_requests, max_tool_calls, max_reported_tokens=None, cancelled=None):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or timeout_s<=0:
            raise ValueError('invalid execution timeout')
        for cap in (max_provider_requests,max_tool_calls):
            if type(cap) is not int or cap<0: raise ValueError('invalid execution allowance')
        if max_reported_tokens is not None and (type(max_reported_tokens) is not int or max_reported_tokens<0):
            raise ValueError('invalid reported token allowance')
        if cancelled is not None and not callable(cancelled): raise ValueError('invalid cancellation callback')
        self._deadline=time.monotonic()+timeout_s
        self._provider_cap,self._tool_cap=max_provider_requests,max_tool_calls
        self._provider_requests=self._tool_calls=self._reported_tokens=0
        self._token_cap=max_reported_tokens
        self._usage_complete=True
        self._cancelled=cancelled
        self._lock=RLock()

    @property
    def deadline(self): return self._deadline
    @property
    def provider_requests(self):
        with self._lock: return self._provider_requests
    @property
    def tool_calls(self):
        with self._lock: return self._tool_calls

    @property
    def reported_tokens(self):
        with self._lock: return self._reported_tokens
    @property
    def usage_complete(self):
        with self._lock: return self._usage_complete

    def _check_tokens(self, *, dispatch=False):
        if self._token_cap is not None:
            if not self._usage_complete: raise InvocationBudgetExceeded('usage_unknown')
            if self._reported_tokens>self._token_cap or (dispatch and self._reported_tokens>=self._token_cap):
                raise InvocationBudgetExceeded('token_limit')

    def check(self):
        if self._cancelled is not None and self._cancelled(): raise Cancelled()
        if time.monotonic()>=self._deadline: raise ToolDeadlineExceeded('execution deadline exceeded')

    @contextmanager
    def activate(self):
        stack=_active.get()
        token=_active.set(stack if self in stack else (*stack,self))
        try: yield self
        finally: _active.reset(token)


def active_budgets(): return _active.get()


def check_budgets(budgets):
    for budget in budgets: budget.check()


def reserve_dispatch(budgets, *, tool=False, reserve=True):
    """All enclosing allowances are checked under ordered locks before any debit."""
    check_budgets(budgets)
    ordered=sorted(budgets,key=id)
    for budget in ordered: budget._lock.acquire()
    try:
        for budget in budgets:
            budget._check_tokens(dispatch=True)
            count=budget._tool_calls if tool else budget._provider_requests
            cap=budget._tool_cap if tool else budget._provider_cap
            if count>=cap:
                raise InvocationBudgetExceeded('tool_call_limit' if tool else 'provider_request_limit')
        if reserve:
            for budget in budgets:
                if tool: budget._tool_calls+=1
                else: budget._provider_requests+=1
    finally:
        for budget in reversed(ordered): budget._lock.release()


def record_shared_usage(budgets, usage):
    # Retain all enclosing paid evidence before reporting any violated cap.
    ordered=sorted(budgets,key=id)
    for budget in ordered: budget._lock.acquire()
    try:
        for budget in budgets:
            if type(usage) is not int or usage<0: budget._usage_complete=False
            else: budget._reported_tokens+=usage
        for budget in budgets: budget._check_tokens()
    finally:
        for budget in reversed(ordered): budget._lock.release()
