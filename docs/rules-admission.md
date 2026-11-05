# Rule admission

Prosaic Core lets prose declare which rules a host may check mechanically, with
inert enforcement tags (see Core's `docs/rules.md`). Core parses, validates and
strips them; it never runs anything. Runtime consumes Core's `rules` output for
conversation turns: it binds each declared guard to one the host registered in
Python, refuses admission when it cannot, and runs bound guards inside the
existing turn loop.

**Off by default.** An agent whose prose has no tags produces byte-identical
requests and identical results, whether or not the host registers guards. This is
pinned by `tests/fixtures/rule_admission/untagged_golden.json`, captured from
Runtime main before this feature existed. A host that registers nothing loses
nothing; it only refuses tagged prose it cannot enforce.

## Registering guards

Guards are host code, registered like `CustomTool`. Markdown binds only what is
registered; a guard name in prose is a reference, never a definition.

```python
from prosaic_runtime import Guard, ProsaicRuntime, Verdict, generic_guards

def shelf_marks(stage, context):
    # context: stage, rule_id, rule_text, source_file, settings, paths,
    # user_message, input_data, tool_results, tool_name, arguments,
    # tool_result, draft. The context is read-only; JSON facts are decoded
    # only when read, as copies private to this check; tool_results is a
    # read-only sequence of the results the model has seen this turn.
    bad = [mark for mark in find_marks(context.draft) if mark not in known_marks()]
    return Verdict(not bad, reason='unknown shelf marks', refused_values=tuple(bad))

guard = Guard('shelf_marks', 'shelf-marks-v1', ('before_answer', 'final'), shelf_marks,
              settings_schema={'type': 'object', 'additionalProperties': False,
                               'properties': {'limit': {'type': 'integer'}}})
runtime = ProsaicRuntime(config, guards=[guard, *generic_guards()])
```

`Verdict(ok, reason=None, refused_values=(), fixed=None)`. `fixed` is the repair:
new arguments (`before_tool`), a new tool payload (`after_tool`) or new answer
text (`before_answer`, `final`). A guard that can repair declares
`fixes=True` next to its stages; admission refuses an `on_violation=fix` binding
on any other guard, so the misconfiguration fails at startup rather than in
production. `settings_schema` must be a closed object schema with explicit
`additionalProperties`; settings are validated against it at admission.
`path_settings` names settings that are paths (see Threat model). `echo_values`
(default `False`) lets a guard's raw refused values appear in answer-stage retry
feedback (see Retry feedback).

Guards are trusted, synchronous host code and must be quick and bounded. Runtime
checks the turn deadline and cancellation before and after every check, so an
overrunning guard ends the turn as soon as it returns, but it cannot preempt a
guard that never returns. Keep each check proportional to the facts it reads;
`value_in_tool_output`, for example, compares each stated figure with each
source figure of the turn.

## Admission

`prepare_turn` (and `turn`, which prepares) refuses with a closed
`TurnAdmissionError` code before any provider request:

| Refusal | Code |
| --- | --- |
| A tag names an unregistered guard with `on_violation=fail` (always), or any unregistered guard unless the host passed `ProsaicRuntime(..., allow_unbound_guards=True)` | `invalidArtifact` |
| Inspection reported `ruleIssues` (a malformed tag may have been meant as a `fail` guard) | `invalidArtifact` |
| A guard bound at a stage it does not support, `on_violation=fix` on a guard without `fixes=True`, settings outside its schema, a path setting outside the project root | `invalidArtifact` |
| Inspection rules that omit a tag Core finds in the body; an installed Core without `parse_rules` and tag-like text | `invalidArtifact` |
| Hostile or malformed rule shapes | `invalidArtifact` |
| Prose lacks a `RuntimeConfig.required_guards` guard, binds any of its bindings weaker, or conflicts with a pinned stage or settings (an unregistered required guard already fails when `ProsaicRuntime` is built) | `invalidPolicy` |

An unbound guard admitted with `allow_unbound_guards` is never evaluated and
never counts as passing: it is absent from `rules_evaluated` but present in the
fingerprint with a null version.

Rules come from inspection (`ProsaicArtifact.from_inspection` keeps `rules` and
`ruleIssues`), cross-checked against Core's own `parse_rules` of the body. An
artifact built without inspection rules is parsed with `parse_rules`. Runtime
never reads raw comments itself; it uses Core's functions so parsing and
stripping cannot drift. The model always receives Core's `strip_tags` text.

`required_guards` is host policy. Each entry is a minimum action, ordered
`fix < retry < fail`, or a mapping that also pins the guard's settings and stage:

```yaml
required_guards:
  strict_json: retry
  args_reference_seen_ids:
    action: fail
    stage: before_tool
    settings: {keys: [recordId]}
```

Prose cannot weaken a required guard:

- The prose must bind the guard, to a registered guard, and every binding of it
  must be at least the minimum action. Prose may tighten the action.
- Pinned values are authoritative. A pinned `settings` mapping replaces the
  prose settings; prose may omit settings or restate pinned keys with the same
  values, and any other key or value is refused. A pinned `stage` must match the
  binding's stage; Core reports an omitted stage as `final`, so authors state a
  pinned stage explicitly. A conflicting binding is refused with
  `TurnAdmissionError.detail` naming the guard and the pin, never prose values.
- Required guards are checked when `ProsaicRuntime` starts: an unregistered
  required guard, a pinned stage the guard does not support, or pinned settings
  outside its schema raise `ValueError` before any turn. A null
  `required_guards` means none.
- `run()` refuses every artifact while `required_guards` is non-empty, because it
  has no rule hooks.
- `required_guards` is a Python-host setting: only a host that registers guards
  (`ProsaicRuntime(guards=...)`) can satisfy it. Keep it out of configs shared
  with the CLI, which cannot register guards; CLI `preflight` ignores it.

**Scope.** `required_guards` applies to every agent the runtime admits. It means
"the prose must bind this guard", not "the host attaches this guard": an agent
that does not tag a required guard is refused. To require a guard for one agent
or workflow only, give that scope its own `ProsaicRuntime` with its own
`RuntimeConfig`. Per-agent or per-workflow scoping, and host-attached guards that
run without a prose tag, are planned follow-ups.

### Several files in one turn

A host may compose several prose files into one turn by supplying their rules
together. Each `(guard, stage)` pair is one binding, so the same guard bound at
`before_tool` in one file and at `final` in another runs at both stages. At the
same stage across files:

- identical bindings (same action and settings) dedupe silently;
- different actions keep the stronger action's binding with its own settings,
  and emit `RuleBindingWarning` naming the stage and both sources;
- the same action with different settings is refused as ambiguous.

`compile_rules(entries, guards, ...)` exposes the same compilation to hosts.

## Hook points

Hooks run inside the existing turn loop. A guard never raises into the loop: an
exception there would look like a provider or observation failure. Any guard
exception, invalid verdict or failed repair is a safe failure.

| Stage | Runs on | `fix` | `retry` | `fail` |
| --- | --- | --- | --- | --- |
| `before_tool` | each call of a batch, before any effect | rewrite arguments, revalidated against the tool schema | the call does not run; the model gets an error result with feedback | the turn fails before any effect |
| `after_tool` | each executed result, before the model sees it | repair the result the model sees | replace the result with error feedback; the tool is not re-run | the turn fails, effect evidence kept |
| `before_answer` | the raw draft | repair the text | ask the model again | the turn fails |
| `final` | the text after `before_answer` repairs | repair the text | ask the model again | the turn fails |

`before_answer` and `final` both run on the model's final answer in the same
phase and differ only in order: `before_answer` rules see the raw draft first,
`final` rules see the text after `before_answer` repairs, and after any repair
every rule of both stages is rechecked on the repaired text. Use `before_answer`
for checks of what the model wrote and `final` for checks of what will be
delivered. `final` is not tied to host terminal completion, which conversation
turns do not support (see Terminal completion).

- **fix** applies repairs in order, then rechecks every rule of the phase. Up to
  three repair passes; still violated after that is a failure. A repair is never
  success on its own, and a `retry` or `fail` is decided only on verdicts judged
  against the final repaired value, never on one a later repair made stale.
- **retry** happens at most once per turn, across all stages, within the existing
  deadline and `RunPolicy` budgets. When no provider request remains it fails
  instead; a retry never adds a request beyond the policy. All refusals of one
  tool batch, before or after its calls, share that one retry, because their
  feedback returns in the same next request.
- **fail** returns a safe failed `TurnResult` with failure code `ruleViolation`.

`before_tool` rewrites are recorded as what ran: the wire message, the tool
journal and `CompletedTurn` carry the rewritten arguments. `after_tool` repairs
change what the model and `CompletedTurn` see; `TurnEvidence` keeps the outcome
that actually happened. A refused call counts as a tool attempt, as a denied call
does, and is evidenced as `not_granted`.

Only the accepted answer enters `CompletedTurn`. A rejected draft and its
feedback stay out of history; `providerAttempts` records the extra request.
Streamed text of a rejected or not-yet-repaired draft was already delivered as
provisional events; `TurnResult` carries the authoritative answer.

Guards see as `tool_results` only what the model saw: a result replaced by retry
feedback is absent, and a repaired result appears in its repaired form. The
result under check at `after_tool` is `tool_result`, not yet in `tool_results`.

### Retry feedback

Feedback always carries the broken rule's text, because a reasonless retry tends
to repeat the same answer. It is an unprivileged message (user role for answers,
a tool error result for tools) in a fixed Runtime envelope, never the
system/instruction role:

```json
{"ruleFeedback": {"version": 1, "violations": [
  {"rule": "State only figures the lookup returned.", "refusedValues": ["GBP 795K"]}]}}
```

Refused values come from the model's draft, which can echo tool or web text, so
in the user role they are sent only for guards registered with
`echo_values=True`; other guards send `"refusedCount": n` instead. Opt in only
when a guard's values cannot carry instructions: `value_in_tool_output` opts in,
because its values are regex-constrained figures such as `EUR 650`. Tool-stage
feedback returns as a tool result and always carries the values.

Guard reasons and tag settings are never included. At most 64 refused values are
sent, and a string value longer than 2,048 characters is cut short with `…`; a
long value never turns a fixable or retryable violation into a failure.

## Turn results

`TurnResult` gains `rules_evaluated`, `rules_violated`, `rules_fixed` and
`rules_faulted` (rule ids only; never rule text, settings or values) and
`rules_fingerprint`. A guard fault (a raise, an invalid verdict or an unusable
repair) fails the turn with `ruleViolation`, like a violation, and its rule id
is also listed in `rules_faulted`, so a guard bug can be told apart from a rule
the model broke.
`rules_violated` lists rules whose violation decided an outcome: a repair, a
retry, a failure, or the last pass before stopping. A verdict made stale by a
later repair in the same pass is not reported. The
fingerprint is SHA-256 over each binding's guard, guard version, source file,
stage, effective action and settings, never the line-based id, so editing above a
rule leaves it unchanged. It is `None` without bindings. Hosts such as Harness
can fold it into workflow fingerprints. `RuleSet.rules_fingerprint()` and
`rules_fingerprint(bindings)` compute the same value.

### Coverage

`ProsaicRuntime.rule_coverage(artifact)` reports, without admission, inference or
a turn, which declared rules name a registered guard, which do not, which are
`prose-only`, and how many tag problems there are (ids and counts only). For a
compiled `RuleSet`, `coverage()` lists enforced, unbound and prose-only rule ids.

```python
runtime.rule_coverage(artifact)
# {'registered': ['agent.md:4:value_in_tool_output'], 'unregistered': [],
#  'proseOnly': ['agent.md:9:prose-only'], 'issues': 0}
```

## Entry points

Rule hooks exist for conversation turns only. `ProsaicRuntime.run`, its
acquisition artifact and `NativeRuntime.run` refuse any artifact with an
`enforce` tag or tag problem, including inspection rules from composed files
when the body itself is untagged: a tagged agent silently running unenforced
would look enforced but not be. For the same reason `ProsaicRuntime.run` refuses
every artifact while `RuntimeConfig.required_guards` is non-empty.
`UnifiedRuntime` applies both refusals itself before any backend, so they are
admission errors rather than uncertain executions. `prose-only` tags declare
rules intentionally left to
the model; such artifacts run everywhere with tags stripped.
`StructuredToolLoop` drives `run` and inherits the refusal.

## Threat model

Rule text, `prose-only` reasons and settings are author data and untrusted.

- **Never executed.** Settings are values from Core's tokenizer (strings,
  integers, flat lists). Runtime validates them against the host's schema and
  passes copies; it never evaluates them. Generic guards never compile an author
  regex: `pattern` is a bounded glob.
- **Never instructions.** The system/instruction role receives only Core's
  `strip_tags` text, so tags, settings and reasons never reach the model. Retry
  feedback quotes the rule sentence, which is author prose the model already has
  in its instructions (tags stripped), so quoting it adds no instruction surface;
  refused values are data inside the fixed envelope above.
- **Paths stay inside the project.** A guard declares its `path_settings`. Each
  such value must be relative, and must resolve (following symlinks) inside the
  Prosaic project directory, the parent of the runtime's `source` directory,
  resolved once when the runtime is built, so a later working-directory change
  cannot move it. The
  absolute resolved path is passed in `context.paths` for enforcement, while the
  turn identity (`PreparedTurn` material) records the project-relative form, so
  the same project at another root, such as another machine or a resumed Harness
  session, keeps its identity. Paths are checked at admission, but a guard
  reads the file later, during the turn: point path settings at files outside
  the agent's writable workspace, or have the guard snapshot the file when the
  turn starts, so an agent with write tools cannot edit the reference it is
  checked against.
- **Hostile tag content cannot crash admission.** Malformed tags, huge values,
  control characters and malformed host-supplied rule shapes produce a closed
  admission code; tests cover these cases.
- **Declarations are not grants.** A tag cannot widen tools, files, network or
  budget, and cannot weaken a guard the host requires.

## Terminal completion

Rule admission follows the same boundary as
[host-owned terminal completion](terminal-completion.md): Runtime owns
execution; hosts own admission. Terminal tools are currently supported only by
`run`, which refuses tagged artifacts, and conversation turns reject terminal
grants, so the two cannot meet today. If `run` gains rule hooks, a final-stage
guard must compose with a terminal tool, not conflict: the terminal output is
host-owned and already validated, so a final-stage guard could only `fail` on it,
never `retry` (which would add the acknowledgement model call terminal
completion removes) or `fix` (which would rewrite a host artifact).

## Generic guards

`generic_guards()` returns four fresh registrations. None is registered by
default; each is inert until a host registers it and prose binds it.

- `no_wrapped_links` (`before_answer`, `final`; `fixes=True`): a complete
  `[text](target)` enclosed in `**`, `__`, `*`, `_` or backtick markers, which
  Markdown-subset renderers show as raw text. The repair removes one wrapping
  layer per pass, so `***[a](/b)***` unwraps within the fix bound. Bare URLs,
  `<autolinks>`, reference-style links and target form are out of scope.
- `value_in_tool_output` (`before_answer`, `final`; no repair): stated figures
  must come from this turn's tool results. In the answer it checks only
  currency-marked amounts and multiples written with `x`; bare numbers are not
  checked (a documented false negative). Sources are numeric fields, text fields
  that are wholly a formatted number, and currency- or x-marked amounts inside
  longer text. Keys whose last word is `id`, `ids`, `reference`, `date` or `year`
  are never read, in any case style (`id`, `homes_id`, `playerId`, `recordIDs`,
  `startDate`, `fiscal_year`); words that merely contain those letters (`paid`,
  `valid`, `identity`) are read. A figure matches when a source rounds or
  truncates to it at its written precision, ignoring sign. A currency named by the
  data (a `currency`/`currencyCode` key on the object or an ancestor, or a text
  amount's marker) must equal the stated one. Markers default to a vendored
  common subset of ISO 4217 codes (`DEFAULT_CODES`, not the full list) plus
  `€ £ $` (read as EUR, GBP, USD), written before the number; the `currencies`
  setting extends them. So `ISO 9001`, `TOP 5` or `API 3` is never read as an
  amount. The subset omits codes that collide with ordinary words (`PHP`, `ALL`,
  `TOP`); add them through `currencies` when the data uses them. Known limit: `$`
  always means USD.
- `args_reference_seen_ids` (`before_tool`; no repair): settings `keys` (required)
  name argument keys that hold ids, at any depth; optional `pattern` (a glob such
  as `REC-*`) marks id-shaped values under any key. Each id must have been seen in
  this turn's tool results, the user message (as a delimited token) or host input
  data. Conversation history never counts. Numbers are read canonically on both
  sides: a whole-number float is its integer text (`4242.0` is `4242`, which JSON
  Schema also accepts as an integer); a bool or a non-whole float under an id key
  is refused.
- `strict_json` (`before_answer`, `final`; no repair): the answer is one strict
  JSON document; duplicate keys, NaN/Infinity, code fences and prose fail.

Only `no_wrapped_links` declares `fixes=True`; binding any of the others with
`on_violation=fix` is refused at admission. A declared fixer that returns no
usable repair on a violation fails the turn.

## Compatibility

- **Core.** Requires Prosaic Core with `parse_rules`/`strip_tags`
  (B3Cognition/prosaic `9d5a02f` or later). Its package version is still 0.4.1,
  the same as the released 0.4.1 that lacks them, so the dependency floor cannot
  express this yet. With an older Core, tagged artifacts are refused and untagged
  ones are unchanged. When Core releases `parse_rules`, the release change is:
  raise the `b3-prosaic` floor in `pyproject.toml` to that version; if it is
  0.5.0 or later, also raise the `<0.5` cap and update
  `tests/test_package_compatibility.py`, which asserts 0.5.0 is excluded; and
  move `publish.yml` and the release scripts off the curated 0.4.1 wheel. CI
  installs Core from git, so it would not catch a range that excludes the
  release.
- **Harness.** Harness 0.9.7 and earlier validate receipt failure codes against a
  closed `_FAILURES` set (`session_contracts.py`). `make_receipt` raises
  `StoreCorrupt('invalid conversation receipt')` **after provider spend** for any
  code outside it, including `ruleViolation`. Hosts binding `fail` or `retry` rules
  under Harness need a Harness release that adds `ruleViolation` to `_FAILURES`, a
  one-line companion change. `fix` rules that succeed and untagged agents never
  produce the code. The new `TurnResult` fields have defaults; Harness constructs
  and checks `TurnResult` by type and existing fields only, so they are safe.
- `ProsaicRuntime.capabilities` includes `rule_admission_v1`.

## Notes for Core maintainers

Production use suggests relaxing Core's duplicate-binding rule from once per file
to once per `(file, stage)`: authors want the same guard at `before_tool` and
`final` in one file, which today requires a second file. Runtime already resolves
bindings per `(guard, stage)`, so that change needs no Runtime change.
