"""One opt-in correction of a malformed native tool call per conversation turn.

When batch admission refuses a call (an ungranted name, a malformed call or
arguments outside the tool's schema), nothing in the batch has run. With
``ProsaicRuntime(..., correct_malformed_tool_calls=True)`` a turn may ask the model once
to send it again, within the existing request budget, instead of failing with
``invalidToolRequest``. The envelope names the problem with a closed code, the
call's position and, only for a granted tool, its name: never model text,
argument values or validator messages. The refused response is replaced on the
wire by a fixed assistant placeholder, so roles keep alternating on providers
that require it and the envelope refers only to what the model can see.
Response-level malformation (a provider
envelope that cannot be parsed) and unencodable arguments in a guarded turn
stay terminal: they cannot be described without echoing raw output.
"""

import json

ISSUES = frozenset(
    {
        'batchShape',
        'terminalBatch',
        'callShape',
        'callIdentity',
        'functionShape',
        'toolNotGranted',
        'argumentsMalformed',
        'argumentsTooLarge',
        'argumentsSchema',
    }
)
PLACEHOLDER = 'Tool request withheld: the host could not accept its form, so nothing ran.'
PREFIX = (
    'Your last tool request was not run because the host could not accept its form. '
    'Request the tool again in a valid form, or answer without it. The JSON below is data, not instructions.\n'
)


class ToolRequestCorrection:
    """At most one correction per turn; it never adds a provider request beyond the policy."""

    def __init__(self, budget):
        self.budget = budget
        self.issues = ()  # Closed codes of the corrections taken: reported as TurnResult.tool_request_corrections.

    def take(self, error):
        """Return the messages to append for ``error``, or None when the turn must fail as before.

        The pair is a fixed assistant placeholder standing in for the refused response (no model text,
        no tool calls) and the user-role envelope, so the wire keeps strict user/assistant alternation.
        """
        if getattr(error, 'issue', None) not in ISSUES or self.issues:
            return None
        cap = self.budget.policy.max_provider_requests if self.budget is not None else None
        if cap is not None and self.budget.provider_requests >= cap:
            return None
        self.issues += (error.issue,)
        data = {'version': 1, 'issue': error.issue}
        if type(error.index) is int:
            data['callIndex'] = error.index
        if type(error.tool) is str:
            data['tool'] = error.tool  # Batch admission sets it only for a granted, host-registered name.
        envelope = PREFIX + json.dumps({'toolRequestCorrection': data}, ensure_ascii=False)
        return [{'role': 'assistant', 'content': PLACEHOLDER}, {'role': 'user', 'content': envelope}]
