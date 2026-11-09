"""Explicit private guard facts, sealed separately from provider input."""
from dataclasses import dataclass, fields
import hashlib
import json
import re
from .conversation import TurnAdmissionError, encode_owned, _closed

INPUT_ACCESS = frozenset({'shared', 'model', 'authoritativeAndModel'})


@dataclass(frozen=True)
class AuthoritativeInputLimits:
    max_bytes: int = 2097152
    max_depth: int = 32
    max_nodes: int = 65536

    def __post_init__(self):
        if any(type(getattr(self,f.name)) is not int or getattr(self,f.name)<=0 for f in fields(self)) or self.max_depth>64:
            raise ValueError('invalid authoritative input limits')


def _profile(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value):
        raise TurnAdmissionError('invalidPolicy')
    return value


def _digest(value):
    return hashlib.sha256(encode_owned(value,max_bytes=8388608)).hexdigest()


def bind_views(ruleset, data, *, input_profile=None, authoritative_input_data=None,
               authoritative_input_profile=None, authoritative_input_limits=None):
    enabled=any(v is not None for v in (input_profile,authoritative_input_data,
        authoritative_input_profile,authoritative_input_limits))
    private=authoritative_input_data is not None
    if not enabled:
        if any(b.bound and b.guard.input_access=='authoritativeAndModel' for b in ruleset.bindings):
            raise TurnAdmissionError('invalidPolicy')
        return None
    _profile(input_profile)
    limits=authoritative_input_limits if authoritative_input_limits is not None else AuthoritativeInputLimits()
    if type(limits) is not AuthoritativeInputLimits:
        raise TurnAdmissionError('invalidPolicy')
    if private:
        _profile(authoritative_input_profile)
        if type(authoritative_input_data) is not dict: raise TurnAdmissionError('inputLimit','authoritativeInputData')
        try:
            owned=json.loads(encode_owned(authoritative_input_data,max_bytes=limits.max_bytes,
                max_depth=limits.max_depth,max_nodes=limits.max_nodes))
        except (TypeError,ValueError,UnicodeError):
            raise TurnAdmissionError('inputLimit','authoritativeInputData') from None
    else:
        if authoritative_input_profile is not None or authoritative_input_limits is not None:
            raise TurnAdmissionError('invalidPolicy')
        owned=None
    guards={}
    for binding in ruleset.bindings:
        if not binding.bound: continue
        guard=binding.guard
        if ((private and guard.input_access=='shared') or
                (not private and guard.input_access=='authoritativeAndModel')):
            raise TurnAdmissionError('invalidPolicy')
        guards[guard.name]=dict(guard=guard.name,version=guard.version,inputAccess=guard.input_access)
    views=dict(version=1,model=dict(profile=input_profile,digest=_digest(data)),
        authoritative=dict(profile=authoritative_input_profile,digest=_digest(owned)) if private else None,
        guardAccess=[guards[k] for k in sorted(guards)])
    views['inputViewsIdentity']=_digest(views)
    return dict(inputProfile=input_profile,authoritativeInputData=owned,
        authoritativeInputProfile=authoritative_input_profile,
        authoritativeInputLimits={f.name:getattr(limits,f.name) for f in fields(limits)} if private else None,
        inputViews=views)


def validate_views(value):
    """Admit private receipt bindings, never the private facts themselves."""
    _closed(value,{'version','model','authoritative','guardAccess','inputViewsIdentity'})
    if type(value['version']) is not int or value['version']!=1: raise ValueError('invalid views version')
    for record in (value['model'],value['authoritative']):
        if record is None:
            if record is value['model']: raise ValueError('missing model view')
            continue
        _closed(record,{'profile','digest'})
        _profile(record['profile'])
        if type(record['digest']) is not str or not re.fullmatch('[0-9a-f]{64}',record['digest']):
            raise ValueError('invalid view digest')
    if type(value['guardAccess']) is not list or len(value['guardAccess'])>256: raise ValueError('invalid access descriptors')
    seen=set()
    for guard in value['guardAccess']:
        _closed(guard,{'guard','version','inputAccess'})
        _profile(guard['guard'])
        if type(guard['version']) is not str or not guard['version'].strip() or len(guard['version'].encode('utf-8'))>128:
            raise ValueError('invalid guard version')
        if (guard['guard'] in seen or type(guard['inputAccess']) is not str or guard['inputAccess'] not in INPUT_ACCESS
                or (value['authoritative'] is not None and guard['inputAccess']=='shared')
                or (value['authoritative'] is None and guard['inputAccess']=='authoritativeAndModel')):
            raise ValueError('invalid access descriptor')
        seen.add(guard['guard'])
    if value['inputViewsIdentity']!=_digest({k:v for k,v in value.items() if k!='inputViewsIdentity'}):
        raise ValueError('invalid views identity')
    return json.loads(encode_owned(value,max_bytes=65536))
