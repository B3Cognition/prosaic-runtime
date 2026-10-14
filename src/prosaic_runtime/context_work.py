"""Bounded active assembly work; provider waiting never consumes this allowance."""
import time
from .conversation import TurnAdmissionError
from .context_contracts import ContextWorkState


class ContextWorkBudget:
    def __init__(self, policy, *, work=None, clock=time.monotonic_ns,
                 checkpoint=None, deadline=None):
        self.policy, self.clock = policy, clock
        self.checkpoint, self.deadline = checkpoint, deadline
        self._state = (work.to_dict() if type(work) is ContextWorkState else {
            'version': 1, 'candidateChecks': 0, 'candidateBytes': 0,
            'activeAssemblyMicros': 0, 'reservedActiveMicros': 0,
            'readmissionCandidateBytes': 0, 'readmissionPending': False,
            'checkpointSequence': 0})
        if work is not None and type(work) is not ContextWorkState:
            raise TurnAdmissionError('invalidPolicy')
        self._check()

    @property
    def state(self):
        return ContextWorkState.from_dict(self._state)

    def _save(self):
        if self.checkpoint is not None:
            self._state['checkpointSequence'] += 1
            self.checkpoint(self.state)

    def _check(self):
        p, s = self.policy, self._state
        if self.deadline is not None and self.clock() / 1e9 >= self.deadline:
            from .tools import ToolDeadlineExceeded
            raise ToolDeadlineExceeded('invocation deadline exceeded')
        if s['candidateChecks'] > p.max_candidate_checks:
            raise TurnAdmissionError('contextLimit', 'maxCandidateChecks')
        if s['candidateBytes'] > p.max_assembly_bytes:
            raise TurnAdmissionError('contextLimit', 'maxAssemblyBytes')
        if s['activeAssemblyMicros'] + s['reservedActiveMicros'] >= p.assembly_timeout_ms * 1000:
            raise TurnAdmissionError('contextLimit', 'assemblyTimeoutMs')

    def construct(self, callback):
        self._check()
        if self.checkpoint is not None:
            self._state['reservedActiveMicros'] = (
                self.policy.assembly_timeout_ms * 1000 - self._state['activeAssemblyMicros'])
            self._save()  # unknown acknowledgement never permits entering host code
        started = self.clock()
        try:
            result = callback()
        except Exception:
            self._finish_active(started)
            raise
        # BaseException/process death deliberately retains the outstanding slice.
        self._finish_active(started)
        return result

    def _finish_active(self, started):
        elapsed = max(0, self.clock() - started)
        self._state['activeAssemblyMicros'] += (elapsed + 999) // 1000
        self._state['reservedActiveMicros'] = 0
        self._save()
        self._check()

    def check_candidate(self, encoded_bytes, callback=None):
        self._check()
        if self._state['candidateChecks'] + 1 > self.policy.max_candidate_checks:
            raise TurnAdmissionError('contextLimit', 'maxCandidateChecks')
        if self._state['candidateBytes'] + encoded_bytes > self.policy.max_assembly_bytes:
            raise TurnAdmissionError('contextLimit', 'maxAssemblyBytes')
        self._state['candidateChecks'] += 1
        self._state['candidateBytes'] += encoded_bytes
        if self._state['readmissionPending']:
            self._state['readmissionPending'] = False
            self._state['readmissionCandidateBytes'] = 0
        if callback is None:
            self._save()
            return None
        return self.construct(callback)

    def reserve_readmission(self, encoded_bytes):
        self._check()
        if self._state['candidateChecks'] + 1 > self.policy.max_candidate_checks:
            raise TurnAdmissionError('contextLimit', 'maxCandidateChecks')
        if self._state['candidateBytes'] + encoded_bytes > self.policy.max_assembly_bytes:
            raise TurnAdmissionError('contextLimit', 'maxAssemblyBytes')
        self._state['readmissionPending'] = True
        self._state['readmissionCandidateBytes'] = encoded_bytes

