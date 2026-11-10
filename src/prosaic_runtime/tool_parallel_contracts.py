"""Explicit host-owned parallel read bounds; never inferred from model output."""
from dataclasses import dataclass


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


def admit_parallel_execution(policy,bindings,*,provider=None,metadata_limit=65536):
    if policy.parallel_tools is None:return
    from .tool_contracts import ToolContractBindingsV1
    if type(policy.parallel_tools) is not ParallelToolPolicyV1 or type(bindings) is not ToolContractBindingsV1:
        raise ValueError('parallel tools require explicit tool contract bindings')
    policy.parallel_tools.__post_init__();bindings.__post_init__()
    if policy.max_provider_requests is None or policy.max_tool_calls is None:
        raise ValueError('parallel tools require finite attempt caps')
    if provider is not None and provider not in {'openai-compatible','anthropic'}:
        raise ValueError('parallel tools require an API provider')
    if 1024+policy.max_tool_calls*1024>metadata_limit:
        raise ValueError('parallel schedule metadata capacity')


def validate_parallel_event(value):
    """A closed observation carries correlation and status, never source payload."""
    from .conversation import encode_owned
    from .tool_outcomes import ToolOutcome
    import json,math,re
    required={'version','batchId','callId','callIndex','batchIndex','state','handlerEntered',
        'journalState','outcomeCode','durationMs'}
    if type(value) is not dict or set(value)!=required or type(value['version']) is not int or value['version']!=1:
        raise ValueError('invalid parallel settlement')
    for key in ('batchId','callId'):
        if type(value[key]) is not str or not re.fullmatch('[0-9a-f]{64}',value[key]):raise ValueError('invalid parallel correlation')
    for key in ('callIndex','batchIndex'):
        if type(value[key]) is not int or value[key]<0:raise ValueError('invalid parallel index')
    if (value['state'] not in {'completed','replayed','uncertain','notStarted'} or
            type(value['handlerEntered']) is not bool or value['journalState'] not in {'notUsed','claimedUncertain','replayed','committed'} or
            type(value['durationMs']) not in (int,float) or not math.isfinite(value['durationMs']) or not 0<=value['durationMs']<=2**63-1):
        raise ValueError('invalid parallel state')
    if value['outcomeCode'] is not None:ToolOutcome(value['outcomeCode'])
    if value['state']=='notStarted' and (value['handlerEntered'] or value['journalState']!='notUsed'):
        raise ValueError('uncertain work is not unstarted')
    if value['state']=='replayed' and (value['handlerEntered'] or value['journalState']!='replayed' or value['outcomeCode'] not in {'ok','controlled_failure'}):
        raise ValueError('invalid parallel replay')
    if value['state']=='completed' and (not value['handlerEntered'] or value['journalState'] not in {'notUsed','committed'} or value['outcomeCode'] not in {'ok','controlled_failure'}):
        raise ValueError('invalid parallel completion')
    return json.loads(encode_owned(value,max_bytes=1024))
