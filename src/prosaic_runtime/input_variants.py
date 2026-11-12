"""One history-first fitting transaction for bounded complete input variants."""
from dataclasses import replace
import json
from .conversation import TurnAdmissionError,TurnLimits,encode_owned
from .context_assembly import assemble_context,context_digest
from .context_contracts import ContextAssemblyReport,ContextSelection
from .context_counters import count_owned
from .context_work import ContextWorkBudget
from .frozen_turns import InputVariantSelection


def prepare_turn_variants(runtime,artifact,*,message,history=(),input_data_variants,policy=None,
                          limits=None,workspace=None,context_policy,token_counters,input_profile=None,
                          authoritative_input_data=None,authoritative_input_profile=None,authoritative_input_limits=None,tool_contract_bindings=None,sub_agent_frame=None):
    limits=TurnLimits() if limits is None else limits
    if type(limits) is not TurnLimits: raise TurnAdmissionError('invalidPolicy')
    try:
        if type(input_data_variants) not in (tuple,list) or not 1<=len(input_data_variants)<=8:
            raise ValueError('variant count')
        values=list(input_data_variants)
        if any(type(v) is not dict for v in values): raise ValueError('variant input must be a mapping')
        values=json.loads(encode_owned(values,max_bytes=min(8388608,limits.max_input_bytes*8),
            max_depth=limits.max_json_depth,max_nodes=limits.max_json_nodes))
        for value in values:
            encode_owned(value,max_bytes=limits.max_input_bytes,max_depth=limits.max_json_depth,max_nodes=limits.max_json_nodes)
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise TurnAdmissionError('inputLimit','inputDataVariants') from None
    if context_policy is None or token_counters is None: raise TurnAdmissionError('invalidPolicy')
    from .conversation_preparation import prepare_turn
    return prepare_turn(runtime,artifact,message=message,history=history,input_data=values[-1],policy=policy,
        limits=limits,workspace=workspace,context_policy=context_policy,token_counters=token_counters,
        input_profile=input_profile if input_profile is not None else 'context-input-variants-v1',
        authoritative_input_data=authoritative_input_data,authoritative_input_profile=authoritative_input_profile,
        authoritative_input_limits=authoritative_input_limits,_input_data_variants=values,tool_contract_bindings=tool_contract_bindings,sub_agent_frame=sub_agent_frame)


def fit_variants(runtime,agent,run_policy,limits,source_history,message,variants,policy,registration,input_profile):
    from .conversation_preparation import initial_payload,native_payload_digest,current_user_text
    budget=ContextWorkBudget(policy)
    last_payload=None
    def build(history,data):
        nonlocal last_payload
        last_payload=initial_payload(runtime,agent,run_policy,history,message,data,
            context_policy=policy,canonical_messages=True)
        return last_payload[-1]
    selected_history,base_report,_,_=assemble_context(source_history,
        lambda history:build(history,variants[-1]),policy,registration,
        replace(limits,max_input_bytes=run_policy.max_input_bytes),budget=budget,finalize=False)
    base_payload=last_payload
    chosen_payload=base_payload
    chosen_index=len(variants)-1
    chosen_count=base_report.input_tokens
    chosen_size=base_report.encoded_input_bytes
    base_size=chosen_size
    limited=False
    def text_size(data):
        return len(json.dumps(current_user_text(message,data),allow_nan=False).encode('utf-8'))
    base_text_size=None
    for index,data in enumerate(variants[:-1]):
        if not budget.can_trial(0,base_size):
            limited=True
            break
        if base_text_size is None:
            base_text_size=budget.construct(lambda:text_size(variants[-1]))
        # Only the exact current user string differs. Both admitted encoders
        # preserve this string; its JSON byte delta gives exact funding before
        # constructing a complete optional request. Verify against actual bytes.
        size_hint=base_size+budget.construct(lambda:text_size(data))-base_text_size
        if not budget.can_trial(size_hint,max(size_hint,base_size)):
            limited=True
            break
        native,size=budget.construct(lambda:(build(selected_history,data),
            len(json.dumps(last_payload[-1],allow_nan=False).encode('utf-8'))))
        if size!=size_hint: raise TurnAdmissionError('bindingMismatch')
        count=budget.check_candidate(size,(lambda:count_owned(registration,native))
            if size<=run_policy.max_input_bytes else None)
        if count is not None and count<=policy.input_capacity_tokens:
            chosen_payload=last_payload;chosen_index=index;chosen_count=count;chosen_size=size
            break
    native_digest=budget.construct(lambda:native_payload_digest(chosen_payload[-1]))
    budget.reserve_readmission(chosen_size)
    report=ContextAssemblyReport.from_dict({**base_report.to_dict(),'inputTokens':chosen_count,
        'encodedInputBytes':chosen_size,'candidateChecks':budget.state.candidate_checks,
        'candidateBytes':budget.state.candidate_bytes})
    selection=ContextSelection.from_dict(dict(version=1,contextPolicyIdentity=context_digest(policy.to_dict()),
        counterDescriptorIdentity=context_digest(registration.descriptor.to_dict()),
        sourceHistoryDigest=report.source_history_digest,selectedHistoryDigest=report.selected_history_digest,
        retainedRange=report.retained_range,omittedRange=report.omitted_range,
        initialReportIdentity=context_digest(report.to_dict())))
    digests=[context_digest(data) for data in variants]
    input_selection=InputVariantSelection.from_dict(dict(version=1,inputProfile=input_profile,
        candidateDigests=digests,selectedIndex=chosen_index,selectedDigest=digests[chosen_index],selectionWorkLimited=limited))
    return (selected_history,report,selection,budget.state,variants[chosen_index],chosen_payload,
            chosen_size,native_digest,input_selection)
