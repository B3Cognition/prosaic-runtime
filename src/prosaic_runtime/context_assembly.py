"""Full-history-first suffix fitting over validated owned neutral turns."""
import hashlib
import json
from .conversation import TurnAdmissionError, encode_owned
from .context_contracts import ContextAssemblyReport, ContextSelection, ContextWorkState
from .context_counters import count_owned
from .context_work import ContextWorkBudget


def context_digest(value):
    return hashlib.sha256(encode_owned(value)).hexdigest()


def _boundaries(history):
    return [index for index, message in enumerate(history) if message['role'] == 'user']


def assemble_context(source_history, build_candidate, policy, registration, limits, *, work=None):
    budget = ContextWorkBudget(policy, work=work)
    boundaries = _boundaries(source_history)
    total, omitted = len(boundaries), 0
    while True:
        start = boundaries[omitted] if omitted < total else len(source_history)
        selected = source_history[start:]
        def construct():
            candidate = build_candidate(selected)
            size = len(json.dumps(candidate, allow_nan=False).encode('utf-8'))
            return candidate, size
        native, size = budget.construct(construct)
        callback = (lambda: count_owned(registration, native)) if size <= limits.max_input_bytes else None
        count = budget.check_candidate(size, callback)
        if count is not None and count <= policy.input_capacity_tokens:
            budget.reserve_readmission(size)
            report = ContextAssemblyReport.from_dict({'version': 1, 'mode': policy.mode,
                'counterIdentity': policy.counter_identity,
                'sourceHistoryDigest': context_digest(source_history),
                'selectedHistoryDigest': context_digest(selected),
                'inputTokens': count, 'countingKind': registration.descriptor.counting_kind,
                'reservedOutputTokens': policy.reserved_output_tokens,
                'safetyMarginTokens': policy.safety_margin_tokens,
                'inputCapacityTokens': policy.input_capacity_tokens,
                'encodedInputBytes': size, 'retainedTurnCount': total - omitted,
                'omittedTurnCount': omitted,
                'retainedRange': {'startTurn': omitted + 1, 'endTurnExclusive': total + 1} if omitted < total else None,
                'omittedRange': {'startTurn': 1, 'endTurnExclusive': omitted + 1} if omitted else None,
                'candidateChecks': budget.state.candidate_checks,
                'candidateBytes': budget.state.candidate_bytes})
            selection = ContextSelection.from_dict({'version': 1,
                'contextPolicyIdentity': context_digest(policy.to_dict()),
                'counterDescriptorIdentity': context_digest(registration.descriptor.to_dict()),
                'sourceHistoryDigest': report.source_history_digest,
                'selectedHistoryDigest': report.selected_history_digest,
                'retainedRange': report.retained_range, 'omittedRange': report.omitted_range,
                'initialReportIdentity': context_digest(report.to_dict())})
            return selected, report, selection, budget.state
        if policy.mode == 'fullHistory' or omitted == total:
            raise TurnAdmissionError('contextLimit',
                'maxInputBytes' if count is None else 'contextWindowTokens')
        omitted += 1


def restore_selection(source_history, policy, registration, selection, report, work):
    try:
        if (type(selection) is not ContextSelection or type(report) is not ContextAssemblyReport
                or type(work) is not ContextWorkState):
            raise ValueError('context records')
        boundaries = _boundaries(source_history)
        omitted = report.omitted_turn_count
        if omitted > len(boundaries):
            raise ValueError('omission range')
        start = boundaries[omitted] if omitted < len(boundaries) else len(source_history)
        selected = source_history[start:]
        expected = {'version': 1, 'contextPolicyIdentity': context_digest(policy.to_dict()),
            'counterDescriptorIdentity': context_digest(registration.descriptor.to_dict()),
            'sourceHistoryDigest': context_digest(source_history),
            'selectedHistoryDigest': context_digest(selected),
            'retainedRange': report.retained_range, 'omittedRange': report.omitted_range,
            'initialReportIdentity': context_digest(report.to_dict())}
        if (selection.to_dict() != expected or report.source_history_digest != expected['sourceHistoryDigest']
                or report.selected_history_digest != expected['selectedHistoryDigest']
                or report.retained_turn_count != len(boundaries) - omitted
                or report.mode != policy.mode or report.counter_identity != policy.counter_identity
                or report.input_capacity_tokens != policy.input_capacity_tokens
                or report.reserved_output_tokens != policy.reserved_output_tokens
                or report.safety_margin_tokens != policy.safety_margin_tokens
                or report.counting_kind != registration.descriptor.counting_kind
                or work.candidate_checks < report.candidate_checks
                or work.candidate_bytes < report.candidate_bytes):
            raise ValueError('selection binding')
        return selected, report, selection, work
    except (ValueError, TypeError, AttributeError):
        raise TurnAdmissionError('bindingMismatch') from None
