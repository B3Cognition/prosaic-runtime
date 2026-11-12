"""Public data-only seam for restoring one selected, profiled turn."""
import hashlib
import json
import re
from .conversation import (_OwnedRecord, _closed, encode_owned, TurnAdmissionError,
                           TurnLimits, validate_conversation_history)
from .context_assembly import context_digest
from .input_views import AuthoritativeInputLimits, _profile, validate_views
from .tool_event_contract import tool_contract_preparation

FROZEN_LIMIT=8388608


def _hash(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('invalid frozen digest')


class InputVariantSelection(_OwnedRecord):
    @classmethod
    def from_dict(cls,value):
        encoded=encode_owned(value,max_bytes=65536,max_depth=16,max_nodes=1024)
        document=json.loads(encoded)
        _closed(document,{'version','inputProfile','candidateDigests','selectedIndex','selectedDigest','selectionWorkLimited'})
        if type(document['version']) is not int or document['version']!=1: raise ValueError('invalid variant version')
        _profile(document['inputProfile'])
        candidates=document['candidateDigests']; index=document['selectedIndex']
        if type(candidates) is not list or not 1<=len(candidates)<=8: raise ValueError('invalid candidates')
        for digest in candidates: _hash(digest)
        if type(index) is not int or not 0<=index<len(candidates): raise ValueError('invalid selected index')
        if document['selectedDigest']!=candidates[index] or type(document['selectionWorkLimited']) is not bool:
            raise ValueError('invalid selected digest')
        return cls._create(encoded)


class FrozenTurnInput(_OwnedRecord):
    @classmethod
    def from_dict(cls,value):
        encoded=encode_owned(value,max_bytes=FROZEN_LIMIT,max_depth=64,max_nodes=65536)
        document=json.loads(encoded)
        _closed(document,{'version','encodingVersion','contextIdentity','inputBytes','message','messageDigest',
            'selectedInputData','inputProfile','authoritativeInputData','authoritativeInputProfile',
            'authoritativeInputLimits','inputViews','inputVariantSelection','sourceHistoryDigest','selectedHistoryDigest',
            'bindings','contextSelection','contextReport','initialContextWork','contextWork',
            'nativePayloadDigest','workspaceDigest','executionAttempt'})
        if type(document['version']) is not int or (document['version'], document['encodingVersion']) not in ((1, 'conversation-turn-v3'), (2, 'conversation-turn-v4'), (3, 'conversation-turn-v5'), (5, 'conversation-turn-v6')):
            raise ValueError('invalid frozen version')
        for name in ('contextIdentity','messageDigest','sourceHistoryDigest','selectedHistoryDigest','nativePayloadDigest','workspaceDigest'):
            _hash(document[name])
        if (type(document['message']) is not str or not document['message'].strip() or
                hashlib.sha256(document['message'].encode('utf-8')).hexdigest()!=document['messageDigest']):
            raise ValueError('invalid frozen message')
        if type(document['inputBytes']) is not int or document['inputBytes']<=0: raise ValueError('invalid input size')
        if type(document['executionAttempt']) is not int or not 1<=document['executionAttempt']<=2**31-1: raise ValueError('invalid execution attempt')
        if document['version'] == 1 or document['inputViews'] is not None:
            _profile(document['inputProfile'])
            selection=InputVariantSelection.from_dict(document['inputVariantSelection']).to_dict()
            if selection['inputProfile']!=document['inputProfile'] or selection['selectedDigest']!=context_digest(document['selectedInputData']):
                raise ValueError('invalid input selection')
            views=validate_views(document['inputViews'])
            if views['model']!={'profile':document['inputProfile'],'digest':context_digest(document['selectedInputData'])}:
                raise ValueError('invalid model view')
            authoritative=document['authoritativeInputData']
            if authoritative is not None:
                if type(authoritative) is not dict: raise ValueError('invalid private input')
                limits=AuthoritativeInputLimits(**document['authoritativeInputLimits'])
                encode_owned(authoritative,max_bytes=limits.max_bytes,max_depth=limits.max_depth,max_nodes=limits.max_nodes)
                if views['authoritative']!={'profile':document['authoritativeInputProfile'],'digest':context_digest(authoritative)}:
                    raise ValueError('invalid private view')
            elif any(document[k] is not None for k in ('authoritativeInputProfile','authoritativeInputLimits')) or views['authoritative'] is not None:
                raise ValueError('invalid private view')
        elif any(document[k] is not None for k in ('inputProfile', 'authoritativeInputData', 'authoritativeInputProfile', 'authoritativeInputLimits', 'inputVariantSelection')):
            raise ValueError('unexpected optional input view')
        bindings=document['bindings']
        _closed(bindings,{'agent','policy','limits','profile','endpoint','tools','cliSandbox'},
                {'rules','contextPolicy','counterDescriptor'} | ({'toolContractV1'} if document['version']>=2 else set()) | ({'parallelToolsV1'} if document['version'] in (3,5) else set())
                | ({'subAgentsV1'} if document['version']==5 else set()))
        if document['version']==5:
            from .sub_agents import SubAgentFrameV1
            if SubAgentFrameV1.from_dict(bindings.get('subAgentsV1')).to_dict() != bindings['subAgentsV1']:
                raise ValueError('noncanonical delegation frame')
        if document['version']>=2:
            from .tool_contracts import validate_bindings_document
            validate_bindings_document(bindings.get('toolContractV1'))
        if document['version']==3 or document['version']==5 and 'parallelToolsV1' in bindings:
            from .tool_parallel_contracts import validate_parallel_binding
            validate_parallel_binding(bindings.get('parallelToolsV1'))
            if bindings['parallelToolsV1']['policy'] != bindings['policy'].get('parallel_tools'):
                raise ValueError('parallel frozen policy drift')
        elif 'parallel_tools' in bindings['policy']:
            raise ValueError('legacy parallel policy')
        limits=TurnLimits(**bindings['limits'])
        encode_owned(document['selectedInputData'],max_bytes=limits.max_input_bytes,
            max_depth=limits.max_json_depth,max_nodes=limits.max_json_nodes)
        if len(document['message'].encode('utf-8'))>limits.max_user_bytes or document['inputBytes']>limits.max_input_bytes:
            raise ValueError('frozen field limit')
        if 'contextPolicy' in bindings:
            from .context_contracts import ContextPolicy,ContextSelection,ContextAssemblyReport,ContextWorkState
            ContextPolicy.from_dict(bindings['contextPolicy'])
            ContextSelection.from_dict(document['contextSelection'])
            ContextAssemblyReport.from_dict(document['contextReport'])
            for key in ('initialContextWork','contextWork'): ContextWorkState.from_dict(document[key])
            _monotone(document['initialContextWork'],document['contextWork'])
        elif any(document[k] is not None for k in ('contextSelection','contextReport','initialContextWork','contextWork')) or 'counterDescriptor' in bindings:
            raise ValueError('unexpected counter material')
        return cls._create(encoded)


def _monotone(before,after):
    for key in ('candidateChecks','candidateBytes','activeAssemblyMicros','checkpointSequence'):
        if after[key]<before[key]: raise TurnAdmissionError('bindingMismatch')
    if after['activeAssemblyMicros']+after['reservedActiveMicros']<before['activeAssemblyMicros']+before['reservedActiveMicros']:
        raise TurnAdmissionError('bindingMismatch')


def export_frozen(prepared):
    if prepared.encoding_version not in ('conversation-turn-v3', 'conversation-turn-v4', 'conversation-turn-v5', 'conversation-turn-v6'): raise TurnAdmissionError('unsupportedCapability')
    value=json.loads(prepared._encoded)
    excluded={'history','selectedHistory','messages','message','inputData','workspace','inputProfile',
        'authoritativeInputData','authoritativeInputProfile','authoritativeInputLimits','inputViews',
        'inputVariantSelection','nativePayloadDigest','contextSelection','contextReport'}
    work=prepared.context_work.to_dict() if prepared.context_work is not None else None
    record=dict(version=5 if prepared.encoding_version=='conversation-turn-v6' else 3 if prepared.encoding_version=='conversation-turn-v5' else 2 if prepared.encoding_version=='conversation-turn-v4' else 1,encodingVersion=prepared.encoding_version,contextIdentity=prepared.context_identity,
        inputBytes=prepared.input_bytes,message=value['message'],messageDigest=hashlib.sha256(value['message'].encode('utf-8')).hexdigest(),
        selectedInputData=value['inputData'],inputProfile=value.get('inputProfile'),
        authoritativeInputData=value.get('authoritativeInputData'),authoritativeInputProfile=value.get('authoritativeInputProfile'),
        authoritativeInputLimits=value.get('authoritativeInputLimits'),inputViews=value.get('inputViews'),
        inputVariantSelection=value.get('inputVariantSelection'),sourceHistoryDigest=context_digest(value['history']),
        selectedHistoryDigest=context_digest(value.get('selectedHistory',value['history'])),
        bindings={k:v for k,v in value.items() if k not in excluded},contextSelection=value.get('contextSelection'),
        contextReport=value.get('contextReport'),initialContextWork=getattr(prepared,'_initial_context_work',work),
        contextWork=work,nativePayloadDigest=value['nativePayloadDigest'],workspaceDigest=context_digest(value['workspace']),
        executionAttempt=getattr(prepared,'_execution_attempt',1))
    return FrozenTurnInput.from_dict(record)


@tool_contract_preparation
def restore_turn(runtime,artifact,*,history=(),frozen_input,policy,limits,context_policy=None,
                 token_counters=None,workspace=None,context_work=None,fresh_attempt=False,tool_contract_bindings=None,sub_agent_frame=None):
    try:
        if type(fresh_attempt) is not bool: raise ValueError('invalid attempt option')
        frozen=FrozenTurnInput.from_dict(frozen_input.to_dict() if type(frozen_input) is FrozenTurnInput else frozen_input).to_dict()
        if frozen['bindings'].get('toolContractV1') != (tool_contract_bindings.to_dict() if tool_contract_bindings is not None else None):
            raise ValueError('tool contract binding')
        if frozen['bindings'].get('subAgentsV1') != (sub_agent_frame.to_dict() if sub_agent_frame is not None else None):
            raise ValueError('delegation binding')
        if fresh_attempt and sub_agent_frame is not None:
            raise ValueError('delegation does not support fresh attempts')
        if type(limits) is not TurnLimits or context_digest(str(workspace) if workspace is not None else None)!=frozen['workspaceDigest']:
            raise ValueError('workspace binding')
        owned_history=validate_conversation_history(history,limits=limits)
        if context_digest(owned_history)!=frozen['sourceHistoryDigest']: raise ValueError('source history binding')
        from .context_contracts import ContextSelection,ContextAssemblyReport,ContextWorkState
        if frozen['contextWork'] is None:
            if context_work is not None: raise ValueError('no execution work')
            work=None
        else:
            current=frozen['contextWork']; initial=frozen['initialContextWork']
            if context_work is not None and type(context_work) is not ContextWorkState:
                raise ValueError('invalid work')
            if fresh_attempt:
                work=ContextWorkState.from_dict(initial)
                if context_work is not None and context_work.to_dict()!=initial: raise ValueError('retry must retain initial charges')
            else:
                work=context_work if context_work is not None else ContextWorkState.from_dict(current)
                if type(work) is not ContextWorkState: raise ValueError('invalid work')
                _monotone(current,work.to_dict())
        from .conversation_preparation import prepare_turn
        private_limits=frozen['authoritativeInputLimits']
        prepared=prepare_turn(runtime,artifact,message=frozen['message'],history=history,input_data=frozen['selectedInputData'],
            input_profile=frozen['inputProfile'],authoritative_input_data=frozen['authoritativeInputData'],
            authoritative_input_profile=frozen['authoritativeInputProfile'],authoritative_input_limits=AuthoritativeInputLimits(**private_limits) if private_limits is not None else None,
            policy=policy,limits=limits,workspace=workspace,context_policy=context_policy,token_counters=token_counters,
            context_selection=ContextSelection.from_dict(frozen['contextSelection']) if frozen['contextSelection'] is not None else None,
            context_report=ContextAssemblyReport.from_dict(frozen['contextReport']) if frozen['contextReport'] is not None else None,
            context_work=work,_warn_rules=False,_frozen_input=frozen,
            _input_variant_selection=InputVariantSelection.from_dict(frozen['inputVariantSelection']) if frozen['inputVariantSelection'] is not None else None,
            tool_contract_bindings=tool_contract_bindings, sub_agent_frame=sub_agent_frame)
        if prepared.context_identity!=frozen['contextIdentity']: raise ValueError('context binding')
        material=json.loads(prepared._encoded)
        if context_digest(material.get('selectedHistory',material['history']))!=frozen['selectedHistoryDigest']:
            raise ValueError('selected history binding')
        restored=export_frozen(prepared).to_dict()
        if restored['bindings']!=frozen['bindings']: raise ValueError('descriptor binding')
        object.__setattr__(prepared,'_initial_context_work',frozen['initialContextWork'])
        object.__setattr__(prepared,'_execution_attempt',frozen['executionAttempt']+(1 if fresh_attempt else 0))
        return prepared
    except TurnAdmissionError:
        raise
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError,OverflowError,RecursionError):
        raise TurnAdmissionError('bindingMismatch') from None
