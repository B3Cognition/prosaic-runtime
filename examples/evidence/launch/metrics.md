# Harbor launch dossier: measurement notebook

Fictional data for an example, not production telemetry. This notebook describes
what was counted and what was not. A successful request, a correct draft, and a
useful handover are different outcomes. Metrics are frozen at noon on 17 June.

## M01 — Request outcomes

The endpoint recorded 2,400 request attempts across ten pilot days. Of these,
2,316 returned a draft, 60 ended in a client-visible timeout, and 24 returned
an explicit server error. These categories are mutually exclusive and sum to
the recorded attempts. They include retries as new attempts. The notebook does
not provide a separate count of unique coordinator tasks. The observed timeout
rate is therefore 60 divided by 2,400, or 2.5 percent. The combined timeout and
server-error rate is 84 divided by 2,400, or 3.5 percent. Returned drafts account
for 96.5 percent of attempts; that is not a measure of draft correctness.

## M02 — Origin of the reliability headline

An earlier dashboard displayed 99.0 percent in a tile labeled "availability."
The tile counted only the 24 explicit server errors as failures out of all
2,400 attempts. It did not count client-visible timeouts in its failure numerator.
The sponsor copied that figure into the summary slide before this distinction
was explained. The arithmetic of the dashboard tile is consistent with its
definition, but its use as an unqualified reliability claim is misleading.
Do not resolve the discrepancy by asserting that one number was fabricated.
Explain what each measure counts and select the measure appropriate to the
decision. Neither measure tells us the eventual outcome of every user task.

## M03 — Timeouts and completed server work

An engineer found a completed server response after the client deadline for
18 of the 60 timeout attempts. The other 42 were not individually traced before
the cutoff. A late server response does not mean the coordinator received a
usable draft. Counting those 18 as ordinary successes would require a different
user-outcome definition and evidence of delivery. It is also possible that
some remaining attempts completed late, but that has not been established.
The notebook intentionally retains the original client-visible outcome counts.
An analyst may discuss the late completions separately without silently changing
the denominator or treating an incomplete investigation as an exhaustive one.

## M04 — Response-time sample

Latency percentiles were calculated from the 2,316 attempts that returned drafts.
The median was 2.8 seconds and the ninety-fifth percentile was 11.2 seconds.
Timeouts and explicit errors were excluded, so these percentiles cannot describe
the experience of every attempt. The median should not be combined with the
timeout rate to create a new synthetic percentile. The client deadline was
twenty seconds during the last eight days, but thirty seconds during the first
two. No comparable percentile series for those two configurations is included.
Changing a deadline can alter observed timeout rates even without a change in
server work, which is one reason chronological comparisons need care.

## M05 — Quality review

Reviewers examined 120 returned drafts selected from the first four pilot days.
One hundred and eight preserved every fact on the review checklist. Nine omitted
a required action or owner, and three introduced an unsupported deadline. These
categories are mutually exclusive within the reviewed sample. The observed
checklist pass proportion is 90 percent. It should not be represented as a
precise population estimate, since selection was not randomized and several
reviewers chose examples they had time to inspect. No scoring disagreement
study was performed. The measured property is checklist completeness for this
sample, not freedom from every possible error in all pilot drafts.

## M06 — Coverage of the quality sample

All 120 reviewed drafts came from the four daytime teams using the standard
template. None came from the two evening teams. No draft based on a ticket
with a restricted attachment was deliberately included. Twenty reviewed drafts
were written by a coordinator who helped design the checklist. This does not
invalidate the observations, but it limits claims about unfamiliar users and
unusual inputs. The briefing target of ninety percent preserving all required
facts is met numerically in the selected sample. It does not follow that every
team, shift, language, or input category meets that target. Report that distinction
without presenting a new unapproved acceptance threshold as existing policy.

## M07 — Preparation-time comparison

Before the pilot, eight coordinators reported an average handover preparation
time of ten minutes based on recollection. During the pilot, a different group
of twelve coordinators estimated an average of six minutes using Harbor. Four
people belonged to both groups, but their individual paired values were not
retained. The headline reduction of forty percent is the difference between
the two group averages divided by the earlier average. That calculation is
arithmetically valid but does not isolate the effect of Harbor. Ticket mix,
coordinator experience, recall bias, and changing workload could all contribute.
There are no stopwatch observations or matched-case controls in this dossier.

## M08 — Copy events and editing

The interface recorded 1,740 copy actions on returned drafts. Multiple copy
actions can occur for the same draft, and no deduplicated task-level acceptance
measure was produced. A copy action does not record whether the copied text
was edited in the destination or ultimately sent. The interface also recorded
620 edits inside its own draft box, but changes made after copying are invisible.
It would be incorrect to subtract edits from copies and call the remainder
"accepted without correction." These event types overlap and operate at different
levels. The logs show interaction with the draft interface, not a complete
account of human review or downstream use of the proposed handover text.

## M09 — Load experiment

A separate forty-minute experiment sent requests at concurrency eight using
short sanitized inputs. It was performed outside the normal daytime peak while
the shared service's other experiment was idle. It produced no client timeouts,
but it did not include long tickets or the planned forty-team traffic pattern.
The test owner called it a connectivity and basic load check. The notebook
contains no ramp test at concurrency sixteen or higher, no scheduled allocation,
and no estimate of the launch-week arrival burst. A recommendation should not
convert a successful small load experiment into proof of capacity for the
proposed rollout simply because the daily request total sounds modest.

## M10 — Input lengths and token measurements

The service logged input character counts but did not record tokenizer-specific
input lengths. The median source contained 2,100 characters and the largest
contained 18,400 characters. These values cannot be compared directly with a
model's advertised token context window. Output-token usage was returned for
most successful requests but not for timeouts. Billing reconciliation is not
complete, and this notebook provides no trustworthy total monetary cost. A
reviewer may request better usage accounting; it should not calculate a precise
price using an assumed rate or silently treat missing usage as zero. The planned
rollout's long-ticket distribution has also not been measured independently.

## M11 — Privacy observations

No privacy complaint appeared in the pilot's shared feedback mailbox during
the ten-day observation window. That mailbox was introduced on day three, and
the training material also directed urgent concerns to a team lead. The notebook
does not consolidate messages sent to those leads. It contains no completed
content audit of submitted tickets. Therefore the zero complaint count is a
narrow observation, not evidence of zero exposures. The quality reviewers were
not instructed to score privacy issues, so their checklist results cannot fill
this gap. Field notes may describe concerns that never became mailbox entries;
such reports should be investigated without assuming they establish prevalence.

## M12 — Proposed monitoring

Operations drafted a dashboard with separate client timeout, explicit server
error, and returned-draft measures. It also proposed sampling ten notes per
team during the first week. These are future controls rather than controls
already operating. The dashboard has a mock-up but no attached alert destination.
The sampling plan does not specify how reviewers would cover weekends or what
happens after a failed review. No statistical justification for ten samples
is offered. A decision memo can recommend an operationally feasible monitoring
plan while keeping its proposed status explicit. It should avoid suggesting
that a dashboard itself supplies response authority or ensures corrective action.

## M13 — Data integrity checks

The total request count was reconciled against the endpoint's daily counter
for all ten days. Four duplicate export rows were removed before calculating
the counts in M01. That correction is already reflected in 2,400 attempts and
must not be applied a second time. A distinct retry remains a separate attempt,
not an export duplicate. Some user identifiers were intentionally pseudonymized,
which prevents a reliable reconstruction of individual preparation histories.
None of these facts licenses an analyst to repair the data by inventing missing
rows. Where task-level attribution is unavailable, keep the analysis at the
request level and state the limitation rather than overstating completeness.

## M14 — What a follow-up study could answer

A follow-up could use matched handover cases, observed preparation times, and
a predeclared quality checklist reviewed across shifts. A separate capacity
exercise could include long inputs and the shared service's competing traffic.
These would answer different questions and should not be treated as substitutes.
The study proposal does not establish the number of observations needed for
a particular confidence claim. It also does not require exposing real restricted
data to test privacy controls; synthetic examples could probe handling behavior.
Owners, recruitment, review time, and stop criteria remain decisions for the
team. The notebook supplies possible next checks, not a completed experiment.

## M15 — Interpretation notes

The strongest supported numerical statements are about the counted requests
and the selected review sample. The weakest are extrapolations about savings,
production capacity, and privacy. A good analysis need not reject every useful
signal because the pilot was imperfect. It can report that coordinators found
drafts usable while explaining what would be needed to quantify that benefit.
Likewise, a failure rate above a preferred target need not prove a particular
technical cause. The notebook records symptoms and measurement methods, not
a controlled causal investigation. Keep those categories distinct when combining
this file with stakeholder claims in the proposal and observations in field notes.
