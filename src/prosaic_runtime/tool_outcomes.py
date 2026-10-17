"""Fixed, public-safe tool evidence; recovery hints never grant dispatch authority."""
from dataclasses import dataclass

_CODES = {
    'ok': ('execution', 'completed', 'none'),
    'invalid_arguments': ('admission', 'not_started', 'correct_arguments'),
    'not_granted': ('admission', 'not_started', 'stop'),
    'authorization_denied': ('authorization', 'not_started', 'stop'),
    'controlled_failure': ('execution', 'completed', 'stop'),
    'handler_error': ('execution', 'unknown', 'reconcile'),
    'invalid_result': ('result', 'unknown', 'reconcile'),
    'result_limit': ('result', 'unknown', 'reconcile'),
    'execution_failed': ('execution', 'unknown', 'reconcile'),
    'tool_identity_conflict': ('journal', 'unknown', 'reconcile'),
    'tool_journal_failed': ('journal', 'unknown', 'reconcile'),
    'tool_effect_uncertain': ('journal', 'unknown', 'reconcile'),
    'unclassified': ('execution', 'unknown', 'reconcile'),
}


@dataclass(frozen=True)
class ToolOutcome:
    code: str
    replayed: bool = False

    def __post_init__(self):
        if (type(self.code) is not str or self.code not in _CODES or type(self.replayed) is not bool
                or self.replayed and self.code not in {'ok', 'controlled_failure'}):
            raise ValueError('invalid tool outcome')

    def to_dict(self):
        self.__post_init__()
        phase, effect, recovery = _CODES[self.code]
        return dict(version=1, code=self.code, phase='journal' if self.replayed else phase,
                    effect=effect, recovery=recovery, replayed=self.replayed)

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'version', 'code', 'phase', 'effect', 'recovery', 'replayed'}
                or type(value['version']) is not int or value['version'] != 1):
            raise ValueError('invalid tool outcome')
        outcome = cls(value['code'], value['replayed'])
        if value != outcome.to_dict():
            raise ValueError('inconsistent tool outcome')
        return outcome
