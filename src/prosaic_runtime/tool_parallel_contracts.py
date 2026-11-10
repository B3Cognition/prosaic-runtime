"""Explicit host-owned parallel read bounds; never inferred from model output."""
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ParallelToolPolicyV1:
    max_concurrency: int = 4
    max_buffered_result_bytes: int = 1_048_576
    max_pending_events: int = 128
    max_pending_event_bytes: int = 65_536

    def __post_init__(self):
        for name, maximum in (('max_concurrency', 16), ('max_buffered_result_bytes', 8_388_608),
                              ('max_pending_events', 2048), ('max_pending_event_bytes', 1_048_576)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('invalid parallel tool policy')

    def to_dict(self):
        self.__post_init__()
        return dict(version=1, maxConcurrency=self.max_concurrency,
            maxBufferedResultBytes=self.max_buffered_result_bytes, maxPendingEvents=self.max_pending_events,
            maxPendingEventBytes=self.max_pending_event_bytes)

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'version', 'maxConcurrency', 'maxBufferedResultBytes',
                'maxPendingEvents', 'maxPendingEventBytes'} or type(value['version']) is not int or value['version'] != 1):
            raise ValueError('invalid parallel tool policy')
        return cls(value['maxConcurrency'], value['maxBufferedResultBytes'], value['maxPendingEvents'], value['maxPendingEventBytes'])
