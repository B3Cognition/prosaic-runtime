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
    def __init__(self, *, timeout_s, max_provider_requests, max_tool_calls, max_reported_tokens=None, cancelled=None,
                 token_counter=None):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or timeout_s<=0:
            raise ValueError('invalid execution timeout')
        for cap in (max_provider_requests,max_tool_calls):
            if type(cap) is not int or cap<0: raise ValueError('invalid execution allowance')
        if max_reported_tokens is not None and (type(max_reported_tokens) is not int or max_reported_tokens<0):
            raise ValueError('invalid reported token allowance')
        if cancelled is not None and not callable(cancelled): raise ValueError('invalid cancellation callback')
        if token_counter is not None:
            from .context_counters import TokenCounterRegistration
            if type(token_counter) is not TokenCounterRegistration or max_reported_tokens is None:
                raise ValueError('token admission requires a trusted counter and token ceiling')
        self._deadline=time.monotonic()+timeout_s
        self._provider_cap,self._tool_cap=max_provider_requests,max_tool_calls
        self._provider_requests=self._tool_calls=self._reported_tokens=0
        self._token_cap=max_reported_tokens
        self._usage_complete=True
        self._token_counter=token_counter
        self._reserved_tokens=0
        self._reservation_breached=False
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

    @property
    def reserved_tokens(self):
        """In-flight or unresolved token reservations; never reported usage."""
        with self._lock: return self._reserved_tokens

    @property
    def token_admission(self):
        """Owned public counter provenance, or None for legacy stop thresholds."""
        return self._token_counter.descriptor.to_dict() if self._token_counter is not None else None

    @property
    def reservation_exceeded(self):
        with self._lock: return self._reservation_breached

    def _check_tokens(self, *, dispatch=False):
        if self._token_cap is not None:
            if not self._usage_complete: raise InvocationBudgetExceeded('usage_unknown')
            if self._reservation_breached: raise InvocationBudgetExceeded('token_reservation_exceeded')
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


def token_reservations(budgets, native_request, endpoint):
    """Count the owned final native input using host-qualified registrations.

    No heuristic fallback: the host guarantees the counter and the provider must
    honor the selected completion cap. Reported usage is not an invoice guarantee.
    """
    strict=[b for b in budgets if b._token_counter is not None]
    if not strict: return {}
    from .context_counters import count_owned
    try:
        from .config import completion_token_parameter
        parameter = completion_token_parameter(endpoint)
        if set(native_request) & {'max_tokens', 'max_completion_tokens', 'max_output_tokens'} != {parameter}:
            raise ValueError('ambiguous output bound')
        output=native_request.get(parameter)
        if type(output) is not int or output<=0: raise ValueError('missing output bound')
        estimates=[]
        for budget in strict:
            counter=budget._token_counter
            if (counter.descriptor.provider,counter.descriptor.model)!=(endpoint.provider,endpoint.model):
                raise ValueError('counter binding mismatch')
            if native_request.get('model')!=endpoint.model: raise ValueError('request model mismatch')
            estimates.append(count_owned(counter,native_request)+output)
        # A strict child also protects capped enclosing legacy scopes.
        return {b:max(estimates) for b in budgets}
    except Exception:
        raise InvocationBudgetExceeded('token_admission') from None


class _Reservation:
    def __init__(self, amounts):
        self.amounts=dict(amounts)
        self.settled=False


def reserve_dispatch(budgets, *, tool=False, reserve=True, token_reservations=None):
    """All enclosing allowances are checked under ordered locks before any debit."""
    check_budgets(budgets)
    ordered=sorted(budgets,key=id)
    for budget in ordered: budget._lock.acquire()
    try:
        for budget in budgets:
            budget._check_tokens(dispatch=True)
            amount=(token_reservations or {}).get(budget,0)
            if type(amount) is not int or amount<0: raise ValueError('invalid token reservation')
            if (not tool and budget._token_cap is not None and
                    budget._reported_tokens+budget._reserved_tokens+amount>budget._token_cap):
                raise InvocationBudgetExceeded('token_limit')
            count=budget._tool_calls if tool else budget._provider_requests
            cap=budget._tool_cap if tool else budget._provider_cap
            if count>=cap:
                raise InvocationBudgetExceeded('tool_call_limit' if tool else 'provider_request_limit')
        if reserve:
            for budget in budgets:
                if tool: budget._tool_calls+=1
                else:
                    budget._provider_requests+=1
                    budget._reserved_tokens+=(token_reservations or {}).get(budget,0)
            return _Reservation(token_reservations or {})
    finally:
        for budget in reversed(ordered): budget._lock.release()


def record_shared_usage(budgets, usage, *, reservation=None):
    # Retain all enclosing paid evidence before reporting any violated cap.
    ordered=sorted(budgets,key=id)
    for budget in ordered: budget._lock.acquire()
    try:
        if reservation is not None and reservation.settled: return
        for budget in budgets:
            if type(usage) is not int or usage<0: budget._usage_complete=False
            else:
                budget._reported_tokens+=usage
                if reservation is not None and budget in reservation.amounts:
                    admitted=reservation.amounts[budget]
                    budget._reserved_tokens-=admitted
                    if usage>admitted: budget._reservation_breached=True
        if reservation is not None: reservation.settled=True
        for budget in budgets: budget._check_tokens()
    finally:
        for budget in reversed(ordered): budget._lock.release()
