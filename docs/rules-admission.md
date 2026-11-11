# Rule admission

The context-pack candidate adds explicit guard input roles. When supplying
private authoritative facts, audit shared guards and bind each to `model` or
`authoritativeAndModel`. Private checks are fail-only and cannot echo or repair
values. See [separate input views](context-packs.md).

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
text (`before_answer`, `final`). A violation left at `fixed=None` declines the
repair, and a `fix` binding then retries (see Hook points). A guard that can repair declares
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
- the same settings with different actions keep the stronger action, and emit
  `RuleBindingWarning` naming the stage and both sources; no check is lost;
- different settings are refused as ambiguous, whatever the actions: keeping
  either binding would silently drop the other's check (`keys=[a]` with
  `retry` in one file, `keys=[b]` with `fail` in another, would never check
  `a`).

`compile_rules(entries, guards, ...)` exposes the same compilation to hosts.

## Hook points

Hooks run inside the existing turn loop. A guard never raises into the loop: an
exception there would look like a provider or observation failure. Any guard
exception, invalid verdict or unusable repair is a safe failure.

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
  A fix guard may **decline**: a violation with no repair (`fixed=None`). The
  rule then takes the `retry` path for that verdict, with the same allowance,
  budgets and feedback as a `retry` binding; with no retry left the turn fails
  with `ruleViolation`. A decline is not a guard fault. A repair of the wrong
  type, too large, not strictly encodable or outside the tool schema still is.
- **retry** happens at most `rule_retries` times per turn (default 1), across
  all stages, within the existing deadline and `RunPolicy` budgets. When no
  provider request remains it fails instead; a retry never adds a request beyond
  the policy. All refusals of one tool batch, before or after its calls, share
  one retry, because their feedback returns in the same next request. Declined
  repairs and citation repairs draw on the same allowance. The host sets it from
  0 (never retry) to 3 with `ProsaicRuntime(config, guards=..., rule_retries=2)`,
  like the guards themselves (not a YAML key); other values are refused when the
  runtime is constructed. A value other than 1 is part of the prepared turn's
  identity, so a turn prepared under one allowance is refused with
  `bindingMismatch` under another; the rules fingerprint is unchanged.
- **fail** returns a safe failed `TurnResult` with failure code `ruleViolation`.

`before_tool` rewrites are recorded as what ran: the wire message, the tool
journal and `CompletedTurn` carry the rewritten arguments. `after_tool` repairs
change what the model and `CompletedTurn` see; `TurnEvidence` keeps the outcome
that actually happened. A refused call counts as a tool attempt, as a denied call
does, and is evidenced as `not_granted`.

Only the accepted answer enters `CompletedTurn`. A rejected draft and its
feedback stay out of history; `providerAttempts` records the extra request.

**Drafts are held back.** While any `before_answer` or `final` rule is bound,
model text is not streamed to observers (`text_delta`/`text` events) or
previewed on the operator console until every answer-stage rule has accepted
it. The accepted (possibly repaired) answer is then delivered as an untagged
turn would deliver it: streamed turns get `text_delta` events (in chunks that
fit `TurnLimits.max_event_bytes`) and the console its usual preview; a refused
or failed draft is never delivered,
so a rule such as "share no contact details" also keeps the draft out of
observers and logs. Text that accompanies tool calls in such a turn is not
streamed either; it stays in `CompletedTurn`. Turns without answer-stage rules
stream as before.

In a turn with bound rules, tool arguments that are not strictly encodable (a
lone surrogate) are a malformed model request on both wires
(`invalidToolRequest`), never a guard fault.

Guards see as `tool_results` only what the model saw: a result replaced by retry
feedback is absent, and a repaired result appears in its repaired form. The
result under check at `after_tool` is `tool_result` (the raw payload), not yet
in `tool_results`.

Each successful result enters `tool_results` as a fact without envelope
bookkeeping, so status fields, offsets, counts, sizes and line numbers never
pass as business facts. Errors, refusals, skipped files and unknown shapes
contribute nothing; effect evidence is unchanged.

| Tool | Fact |
| --- | --- |
| Custom and CLI tools | the `result` value |
| `read_file` | `{"path", "lines"}`: content lines without their `N: ` prefixes |
| `read_many_files` | `{"files": [{"path", "lines"}]}` for files read successfully |
| `grep_files` | `{"matches": [{"path", "text"}]}` |
| `grep_context` | `{"matches": [{"path", "lines"}]}` |
| `list_files` | `{"paths": [path, ...]}` |
| `list_tree_with_sizes` | `{"paths": [path, ...]}` |
| `sha256_file` | `{"path", "sha256"}` |
| `write_file`, `edit_file` | none: their only data is the model's own input |

A line without its `N: ` prefix is a fragment and is dropped, as is the last
line of a `read_many_files` file whose content a character budget may have cut.

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

Guard reasons and tag settings are never included. The whole envelope stays
within 16 KB, so a retry always fits the turn's limits: at most 64 refused
values per rule, a string value longer than 2,048 characters is cut short with
`…`, rule text is quoted up to 512 characters (halved, down to 16, when many
rules are refused together; the full rule is in the instructions), and values
fill the remaining budget in order. An entry that could not carry every value
also states `"refusedCount": n`; if even the shortened rules do not fit, the
last ones are left out and `"omittedViolations": n` counts them. Long or numerous values never turn a fixable or
retryable violation into a failure.

Refused values are validated in the exact form feedback delivers, while the
check is still inside the guard-fault boundary. A value that could not be
delivered is a guard fault: a type other than string, integer or finite float;
an integer of more than 2,048 digits (or past the interpreter's conversion
limit); or
text that is not strictly encodable, such as a lone surrogate. The turn stops
with `ruleViolation` and the rule in `rules_faulted`, before any further
provider request; an `after_tool` fault keeps the executed call's effect
evidence. Admission likewise refuses rule text, ids or file names that are not
strictly encodable, since they appear in feedback and results.

## Turn results

`TurnResult` gains `rules_evaluated`, `rules_violated`, `rules_fixed`,
`rules_faulted`, `rules_declined` and `rules_retried` (rule ids only; never rule
text, settings or values) and `rules_fingerprint`. A guard fault (a raise, an
invalid verdict, undeliverable feedback values or an unusable repair) fails the
turn with `ruleViolation`, like a violation, and its rule id is also listed in
`rules_faulted`, so a guard bug can be told apart from a rule the model broke.
`rules_declined` lists fix rules whose guard declined to repair (never also a
fault on that account); `rules_retried` lists rules whose violation was answered
with a granted retry, whether bound as `retry` or declined as `fix`;
`rule_retries_used` counts the retries the turn spent, citation repairs included.
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
  Markdown-subset renderers show as raw text. A link may carry a title
  (`[a](/b "Title")`) and its target may hold balanced parentheses
  (`/wiki/Shelf_(north)`) or be written `<...>`. The repair removes one
  wrapping layer per pass, so `***[a](/b)***` unwraps within the fix bound.
  Code is never rewritten: fenced code blocks and inline code spans are left
  alone, except a single-backtick span that is exactly one link, the wrapped
  form itself. Bare URLs, `<autolinks>`, reference-style links and target form
  are out of scope.
- `value_in_tool_output` (`before_answer`, `final`; no repair): stated figures
  must come from this turn's tool results. In the answer it checks
  currency-marked figures and multiples written with `x`; bare numbers are not
  checked (a documented false negative). Codes may also follow their number
  (`5M EUR`); suffix `euro`/`euros` denotes EUR and `cent`/`cents` denotes
  hundredths without inventing a currency code. These fixed words are
  case-insensitive whole words, so `centipedes` and `European` are not markers.
  - **Markers.** The ISO codes of `DEFAULT_CODES` (a vendored common subset,
    not the full list) in upper case, or in lower or title case except the
    codes that are ordinary words (`TRY`, `MAD`, `PEN`, `COP`, `RON`, `CAD`,
    `RUB`), so `try 5 times` is never an amount; codes added through the
    `currencies` setting, as written; `€ £ $` (read as EUR, GBP, USD; `$`
    always means USD) before or after the number (`€12`, `12 €`); and the
    prefixed dollars `US$ A$ AU$ C$ CA$ HK$ NZ$ S$ R$ MX$` (also accepted as
    suffixes). The subset omits
    codes that collide with ordinary words (`PHP`, `ALL`, `TOP`); add them
    through `currencies` when the data uses them. `ISO 9001`, `TOP 5` or
    `API 3` is never read as an amount.
  - **Figures.** Up to 32 characters of spacing, Markdown emphasis, code
    ticks or `~`/`≈` may stand between marker and number (`EUR **5M**`); a
    line break only when the digits follow directly (`EUR` then `999M` on the
    next line), so a bullet on the next line is never part of the figure. Digits may group thousands with `,`
    `.` `'` `’` or spaces (`EUR 1 000 000`, `CHF 1'000`) and use `.` or `,`
    for decimals (`EUR 1.234,56`); `1,500` is tried both ways, and a leading
    point is a decimal (`$.75`). Scales: `k`, `m`/`M`/`mm`/`mn`/`mln`/`mio`/
    `million`/`Millionen`, `b`/`B`/`bn`/`bln`/`mrd`/`billion`/`Milliarde(n)`,
    `T`/`tn`/`trillion`, `hundred`, `thousand`, `lakh` and `crore`; a letter
    touches the digits (`€5m`) or, in upper case, follows one space (`€5 M`);
    a word may follow spacing, emphasis or a hyphen (`€1.4 million`,
    `**€1.4** million`, `$5-million`). A marked figure this grammar cannot read
    (`EUR 1.2.3`, `EUR 12abc`, `$5_000`) or a range (`$3-5m`, `$3 to 5m`) is
    refused, never skipped or read as a prefix.
    A shared marker cannot hide a preceding suffix amount (`1.20 EUR` followed
    by a newline and `30 days`). Conflicting units on one number and unsupported
    compounds such as `120 cents USD` are refused and give no source evidence;
    use a supported single-marker spelling or trusted currency metadata.
  - **Signs.** A sign may stand before the number, or before the marker when
    it touches the marker (`-$5`, `-**$5**`) and follows no word character, so
    a Markdown bullet (`- $5m`) or a range (`$10-$20`) is never a minus (ASCII
    plus and minus, the Unicode minus, en and em dashes, hyphens);
    accounting parentheses after the marker (`$(12)`) are negative, while
    parentheses around the whole amount (`(EUR 12)`) are prose. By default the
    sign must agree with the source, so a loss of −12M does not support a
    stated `EUR 12M`; the setting `sign=magnitude` compares magnitudes instead.
    A hyphenated code such as `EUR-2024` reads as a negative amount.
    A line-start Markdown `- ` bullet before a suffixed amount is positive;
    a touching sign (`-120 cents`) is negative.
  - **Sources.** Numeric fields, text fields that are wholly a figure, and
    marked figures or x-multiples inside longer text. Keys whose last word is
    `id`, `ids`, `reference`, `date`, `year`, `path`, `paths`, `url` or `urls`,
    and `sha256`, are never read, in any case style (`id`, `homes_id`,
    `playerId`, `recordIDs`, `startDate`, `fiscal_year`); words that merely
    contain those letters (`paid`, `valid`, `identity`) are read. A currency
    named by the data (a `currency`/`currencyCode` key on the object or an
    ancestor, or a text figure's marker) must equal the stated one. A cent text
    source inherits an enclosing currency field when present. Money text does
    not prove an x-multiple, and an x-multiple does not prove a monetary amount.
  - **Matching.** A figure matches a source that rounds to it, half up or half
    even, at its written precision and scale; truncation does not count, so
    `EUR 1M` is not supported by 1.9M.
  - **Upgrade identity.** Runtime 0.9.15 advances this guard's version to
    `value-in-tool-output-v2`. Its rules fingerprint changes; hosts must
    explicitly readmit affected bindings rather than silently resume under a
    different scanner. Other guards and untagged execution are unchanged.
- `args_reference_seen_ids` (`before_tool`; no repair): settings `keys`
  (required) name argument keys that hold ids, at any depth; optional `pattern`
  (a glob such as `REC-*`) marks id-shaped values under any key: a whole value
  without whitespace, a word inside longer text (`"id REC-999"`,
  `"close INV.2024.77"`, `"(ORD/12)"`), or an object key
  (`{"REC-9": {...}}`).
  Every referenced value is checked, whatever its length. An empty or
  whitespace-only string is never an id and is refused, whatever the inputs
  contain; so is a bool or a non-whole float under an id key. A whole-number
  float is its integer text (`4242.0` is `4242`, which JSON Schema also accepts
  as an integer). An id is seen this turn when:
  - a text id equals any value in the tool results (as guards see them, above)
    or host input data;
  - a numeric id equals a value under an id key there: a key named in `keys`,
    or whose last word is `id`, `ids`, `ref`, `refs`, `reference` or
    `references`. A count, page number or price is never evidence of a numeric
    id;
  - a text id appears as a whole word (delimited by anything other than a
    letter, digit, `_`, `-` or combining mark, in any script, after NFC
    normalization) in the user message or in file lines read by built-in tools
    (`ToolResults.builtin`); a custom result's `text` or `lines` field is not
    file text. Ids without a letter or digit never match this way;
  - a numeric id appears in the user message right after an id cue: `#12`,
    `id 12`, `ID: 12`, or a word of an id key (`record 12` or `records 12` for
    `recordId`). `Page 1` is not a reference to id 1.

  Object keys of results or input data, prose inside custom tool results and
  conversation history never count. Each text is searched separately and the
  work per check is bounded; a search past the bound fails closed.
- `strict_json` (`before_answer`, `final`; no repair): the answer is one strict
  JSON document; duplicate keys, NaN/Infinity, code fences and prose fail.

Only `no_wrapped_links` declares `fixes=True`; binding any of the others with
`on_violation=fix` is refused at admission. A declared fixer that returns no
usable repair on a violation fails the turn.

## Compatibility

- **Core.** Uses Prosaic Core's `parse_rules`/`strip_tags`, first released in
  Core 0.5.0; the declared dependency `b3-prosaic>=0.5.0,<0.6` guarantees them.
  If an older Core is installed anyway, tagged artifacts are refused and
  untagged ones are unchanged.
- **Release gate.** `scripts/rule_admission_wheel_smoke.py` runs a tagged agent
  from the installed wheels: coverage, an enforced retry, tag stripping and an
  unregistered `fail` guard refused. It fails unless the installed Core provides
  `parse_rules`. Release qualification (`publish.yml`, isolated-wheel and
  dependency-floor environments) runs it against the curated, receipt-verified
  Core 0.5.0 wheel that satisfies the declared dependency, and PR CI runs it
  against the same Core release commit (`67b3807`).
- **Harness: guarded conversations are unsupported** until a companion Harness
  release. Against Harness main `797d03c` (0.9.7), its conversation path drops
  enforcement requirements before Runtime sees them:
  - `factory._config_payload` and `_config_from_json` do not carry
    `required_guards`, so a rebuilt `RuntimeConfig` has none and an untagged
    agent is admitted;
  - `factory._artifact_data` drops `rules` and `ruleIssues`, so rules supplied
    by inspection from composed files are lost when the primary body is
    untagged;
  - conversation bindings build `ProsaicRuntime(config, custom_tools=...)`
    without `guards`, so a tagged agent is refused rather than enforced;
  - receipt failure codes are a closed `_FAILURES` set
    (`session_contracts.py`), so `make_receipt` raises
    `StoreCorrupt('invalid conversation receipt')` for `ruleViolation` **after
    provider spend**.

  The companion change must preserve the effective `required_guards` and the
  complete inspection data (`rules`, `ruleIssues`) through snapshots, admission
  and readmission; carry host guard registrations into every Runtime it builds,
  binding guard identity and version wherever execution identity needs it
  (`rules_fingerprint` is available for this); and admit and persist
  `ruleViolation`. Untagged agents are unaffected. The new `TurnResult` fields
  have defaults; Harness constructs and checks `TurnResult` by type and existing
  fields only, so they are safe.
- `ProsaicRuntime.capabilities` includes `rule_admission_v1`.

## Notes for Core maintainers

Production use suggests relaxing Core's duplicate-binding rule from once per file
to once per `(file, stage)`: authors want the same guard at `before_tool` and
`final` in one file, which today requires a second file. Runtime already resolves
bindings per `(guard, stage)`, so that change needs no Runtime change.
