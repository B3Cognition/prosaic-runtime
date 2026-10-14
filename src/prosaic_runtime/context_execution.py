"""Invocation-local exact native envelope guards, before accounting and HTTP."""
import json
from .conversation import TurnAdmissionError
from .context_contracts import ContextAssemblyReport
from .context_counters import count_owned
from .context_assembly import context_digest
from .context_work import ContextWorkBudget


class ContextExecutionFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ContextExecutionGuard:
    def __init__(self, policy, registration, report, work, *, input_byte_cap,
                 checkpoint=None, deadline=None):
        self.policy, self.registration, self.initial_report = policy, registration, report
        self.input_byte_cap = input_byte_cap
        self.budget = ContextWorkBudget(policy, work=work, checkpoint=checkpoint, deadline=deadline)
        self._first_wire_digest = None

    @property
    def work(self):
        return self.budget.state

    @property
    def report(self):
        return ContextAssemblyReport.from_dict({**self.initial_report.to_dict(),
            'candidateChecks': self.work.candidate_checks,
            'candidateBytes': self.work.candidate_bytes})

    def readmit(self, build_native):
        def construct():
            native = build_native()
            return native, len(json.dumps(native, allow_nan=False).encode('utf-8'))
        native, size = self.budget.construct(construct)
        count = self.budget.check_candidate(size, lambda: count_owned(self.registration, native))
        if (size != self.initial_report.encoded_input_bytes or count != self.initial_report.input_tokens
                or size > self.input_byte_cap or count > self.policy.input_capacity_tokens):
            raise TurnAdmissionError('bindingMismatch')
        self._first_wire_digest = context_digest(native)

    def construct_native(self, callback):
        """Charge local construction/encoding, excluding provider/storage waits."""
        try:
            return self.budget.construct(callback)
        except TurnAdmissionError as exc:
            raise ContextExecutionFailure('contextLimit' if exc.code == 'contextLimit' else 'bindingMismatch') from None

    def check_wire(self, native, *, deadline, encoded_bytes):
        try:
            self.budget.deadline = min(self.budget.deadline, deadline) if self.budget.deadline is not None else deadline
            self.budget._check()
            if type(native.get('max_tokens')) is not int or not 0 < native['max_tokens'] <= self.policy.reserved_output_tokens:
                raise TurnAdmissionError('bindingMismatch')
            if self._first_wire_digest is not None:
                expected, self._first_wire_digest = self._first_wire_digest, None
                if context_digest(native) != expected:
                    raise TurnAdmissionError('bindingMismatch')
                return
            if encoded_bytes > self.input_byte_cap:
                self.budget.check_candidate(encoded_bytes)
                raise TurnAdmissionError('contextLimit', 'maxInputBytes')
            count = self.budget.check_candidate(encoded_bytes, lambda: count_owned(self.registration, native))
            if count > self.policy.input_capacity_tokens:
                raise TurnAdmissionError('contextLimit', 'contextWindowTokens')
        except TurnAdmissionError as exc:
            raise ContextExecutionFailure('contextLimit' if exc.code == 'contextLimit' else 'bindingMismatch') from None
