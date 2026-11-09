"""Host rule admission over Core's inert enforcement tags.

Core parses, validates and strips tags; it never runs a guard. Runtime binds
each declared guard to one the host registered in Python, refuses admission
when it cannot, and never treats an unbound guard as passing. Rule text,
reasons and settings are untrusted author data: never executed, never sent as
instructions. See docs/rules-admission.md.

Types live in rule_types.py and required-guard policy in rule_policy.py; both
are re-exported here.
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import re
import warnings

from .rule_policy import apply_pins, check_required_pins, required_guard_specs
from .rule_types import (
    ACTIONS,
    MAX_RULES,
    MAX_SETTINGS_BYTES,
    MAX_TEXT_BYTES,
    NAME,
    STAGES,
    STRENGTH,
    Guard,
    GuardContext,
    RuleAdmissionError,
    RuleBindingWarning,
    ToolResults,
    Verdict,
    settings_ok,
    validate_guards,
    validate_settings,
)

__all__ = [
    'ACTIONS', 'NAME', 'STAGES', 'STRENGTH', 'Binding', 'Guard', 'GuardContext', 'RuleAdmissionError',
    'RuleBindingWarning', 'RuleSet', 'ToolResults', 'Verdict', 'artifact_rules', 'check_required_pins',
    'compile_rules', 'refuse_unenforced', 'required_guard_specs', 'rule_coverage', 'rules_fingerprint', 'strip_for_model',
    'turn_rules', 'validate_guards',
]  # fmt: skip

# Mirrors Core's lexical pre-check. Used only when the installed Core predates
# parse_rules, to fail closed for anything that might be a tag.
_LEXICAL_TAG = re.compile(r'<!--[ \t]*(?:enforce|prose-only)(?![A-Za-z0-9_-])')
_ENTRY_KEYS = frozenset({'id', 'kind', 'guard', 'stage', 'action', 'settings', 'reason', 'source', 'text'})


def _core_api():
    try:
        from prosaic import parse_rules, strip_tags
    except ImportError:
        return None
    return parse_rules, strip_tags


def strip_for_model(text):
    """Core's strip_tags: the exact text every distribution target receives."""
    api = _core_api()
    return text if api is None else api[1](text)


@dataclass(frozen=True)
class Binding:
    rule_id: str
    guard_name: str
    guard: Guard | None
    stage: str
    action: str
    settings: dict
    text: str
    file: str
    paths: dict  # Absolute, resolved inside the project root: used for enforcement.
    portable_paths: dict | None = None  # Project-relative: used in turn identity.

    @property
    def bound(self):
        return self.guard is not None


@dataclass(frozen=True)
class RuleSet:
    bindings: tuple = ()
    prose_only: tuple = ()
    warnings: tuple = ()

    @property
    def empty(self):
        return not self.bindings and not self.prose_only

    def at(self, stage):
        return tuple(b for b in self.bindings if b.bound and b.stage == stage)

    def rules_fingerprint(self):
        """Hash binding fields, never the line-based id. None without bindings."""
        return rules_fingerprint(self.bindings)

    def material(self):
        return {
            'bindings': [
                {
                    'id': b.rule_id,
                    'guard': b.guard_name,
                    'version': b.guard.version if b.bound else None,
                    **({'inputAccess':b.guard.input_access} if b.bound and b.guard.input_access!='shared' else {}),
                    'file': b.file,
                    'stage': b.stage,
                    'action': b.action,
                    'settings': b.settings,
                    # Project-relative, so a moved or resumed project keeps its identity.
                    'paths': b.portable_paths or {},
                }
                for b in self.bindings
            ],
            'proseOnly': [entry['id'] for entry in self.prose_only],
            'fingerprint': self.rules_fingerprint(),
        }

    def coverage(self):
        """Which compiled rules are enforced, admitted unbound, or left to the model (ids only)."""
        return {
            'enforced': [b.rule_id for b in self.bindings if b.bound],
            'unbound': [b.rule_id for b in self.bindings if not b.bound],
            'proseOnly': [entry['id'] for entry in self.prose_only],
        }


def rule_coverage(artifact, guards):
    """Report enforcement coverage without admission or a turn; never refuses, ids and counts only.

    ``registered`` rules name a guard the host registered; admission may still
    refuse them (for example, invalid settings). ``unregistered`` rules would be
    refused, or admitted unbound with allow_unbound_guards.
    """
    registry = guards if isinstance(guards, dict) else validate_guards(guards)
    entries, issues = artifact_rules(artifact)
    enforce = [entry for entry in entries if entry.get('kind') == 'enforce']
    return {
        'registered': [entry['id'] for entry in enforce if entry.get('guard') in registry],
        'unregistered': [entry['id'] for entry in enforce if entry.get('guard') not in registry],
        'proseOnly': [entry['id'] for entry in entries if entry.get('kind') == 'prose_only'],
        'issues': len(issues),
    }


def rules_fingerprint(bindings):
    if not bindings:
        return None
    identity = sorted(
        json.dumps(
            {
                'guard': b.guard_name,
                'version': b.guard.version if b.bound else None,
                **({'inputAccess':b.guard.input_access} if b.bound and b.guard.input_access!='shared' else {}),
                'file': b.file,
                'stage': b.stage,
                'action': b.action,
                'settings': b.settings,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        )
        for b in bindings
    )
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode('utf-8')).hexdigest()


def _encodable(text):
    """Strict UTF-8 bytes; a lone surrogate could never reach feedback or evidence."""
    try:
        return len(text.encode('utf-8'))
    except UnicodeError as exc:
        raise RuleAdmissionError() from exc


def _entry(value):
    """Validate one inspection entry exactly as Core shapes it."""
    if type(value) is not dict or set(value) != _ENTRY_KEYS:
        raise RuleAdmissionError()
    kind, source = value['kind'], value['source']
    if (
        kind not in ('enforce', 'prose_only')
        or type(value['id']) is not str
        or type(value['text']) is not str
        or not value['text']
        or type(source) is not dict
        or set(source) != {'file', 'line'}
        or type(source['file']) is not str
        or not source['file']
        or type(source['line']) is not int
        or source['line'] < 1
        or type(value['settings']) is not dict
    ):
        raise RuleAdmissionError()
    # Rule text is quoted in feedback; the id and file name appear in results and evidence.
    if _encodable(value['text']) > MAX_TEXT_BYTES:
        raise RuleAdmissionError()
    _encodable(value['id'])
    _encodable(source['file'])
    if type(value['reason']) is str:
        _encodable(value['reason'])
    if kind == 'prose_only':
        if (value['guard'], value['stage'], value['action'], value['settings']) != (None, None, None, {}) or (
            type(value['reason']) is not str
        ):
            raise RuleAdmissionError()
        return value
    if (
        type(value['guard']) is not str
        or not NAME.fullmatch(value['guard'])
        or value['stage'] not in STAGES
        or value['action'] not in ACTIONS
        or value['reason'] is not None
    ):
        raise RuleAdmissionError()
    if not settings_ok(value['settings']):
        raise RuleAdmissionError()
    try:
        if len(json.dumps(value['settings'], ensure_ascii=False).encode('utf-8')) > MAX_SETTINGS_BYTES:
            raise RuleAdmissionError()
    except (ValueError, TypeError) as exc:  # Includes integers beyond conversion limits.
        raise RuleAdmissionError() from exc
    return value


def _resolve_paths(guard, settings, project_root):
    """Return (absolute paths for enforcement, project-relative paths for identity)."""
    paths, portable = {}, {}
    for key in guard.path_settings:
        if key not in settings:
            continue
        if project_root is None:
            raise RuleAdmissionError()
        root = Path(project_root).resolve()
        values = settings[key] if type(settings[key]) is list else [settings[key]]
        resolved, relative = [], []
        for value in values:
            if (
                type(value) is not str
                or not value
                or '\x00' in value
                or PurePosixPath(value).is_absolute()
                or PureWindowsPath(value).is_absolute()
                or PureWindowsPath(value).drive
            ):
                raise RuleAdmissionError()
            candidate = (root / value).resolve()
            if not candidate.is_relative_to(root):
                raise RuleAdmissionError()
            resolved.append(str(candidate))
            relative.append(candidate.relative_to(root).as_posix())
        listed = type(settings[key]) is list
        paths[key] = resolved if listed else resolved[0]
        portable[key] = relative if listed else relative[0]
    return paths, portable


def _same_settings(left, right):
    """Settings equality as JSON sees it: 1, 1.0 and true are different values."""
    return json.dumps(left['settings'], sort_keys=True) == json.dumps(right['settings'], sort_keys=True)


def _effective(entries):
    """Resolve bindings per (guard, stage): different stages are separate checks."""
    effective, order, notices, prose, seen = {}, [], [], [], set()
    for entry in entries:
        if entry['kind'] == 'prose_only':
            prose.append(entry)
            continue
        name, file, stage = entry['guard'], entry['source']['file'], entry['stage']
        if (name, file, stage) in seen:  # Core binds each guard once per file.
            raise RuleAdmissionError()
        seen.add((name, file, stage))
        key = (name, stage)
        current = effective.get(key)
        if current is None:
            effective[key] = entry
            order.append(key)
        elif current['action'] == entry['action'] and _same_settings(current, entry):
            continue  # Identical duplicate at this stage: silent.
        elif not _same_settings(current, entry):
            # Different settings at one stage: keeping either binding would silently drop the
            # other's check, whatever the actions, so the composition is ambiguous.
            raise RuleAdmissionError()
        else:  # Same settings, different actions: the stronger action loses no check.
            stronger = entry if STRENGTH[entry['action']] > STRENGTH[current['action']] else current
            notices.append(
                {
                    'code': 'duplicateBinding',
                    'guard': name,
                    'stage': stage,
                    'action': stronger['action'],
                    'sources': [current['source']['file'], file],
                }
            )
            effective[key] = stronger
    return [effective[key] for key in order], prose, notices


def compile_rules(
    entries, guards, *, allow_unbound=False, required_guards=None, project_root=None, issues=(), warn=True
):
    """Compile inspection ``rules`` (possibly from several files) into a RuleSet.

    Refuses on any rule issue, unbound guard (always for fail; otherwise unless
    allow_unbound), unsupported stage, a fix binding on a guard that never fixes,
    invalid settings, a path setting outside the project, ambiguous duplicates,
    or a required guard that is missing, weaker or conflicts with a host pin.
    """
    registry = guards if isinstance(guards, dict) else validate_guards(guards)
    if issues:
        raise RuleAdmissionError()
    if type(entries) not in (list, tuple) or len(entries) > MAX_RULES:
        raise RuleAdmissionError()
    specs = required_guard_specs(required_guards)
    chosen, prose, notices = _effective([_entry(entry) for entry in entries])
    bindings = []
    for entry in chosen:
        name = entry['guard']
        guard = registry.get(name)
        settings = apply_pins(name, entry, specs.get(name))
        if guard is None:
            if entry['action'] == 'fail' or not allow_unbound:
                raise RuleAdmissionError()
            paths, portable = {}, {}
        else:
            if entry['stage'] not in guard.stages:
                raise RuleAdmissionError()
            if entry['action'] == 'fix' and not guard.fixes:
                raise RuleAdmissionError()  # Misconfiguration fails at startup, not in production.
            if guard.input_access=='authoritativeAndModel' and entry['action']!='fail':
                raise RuleAdmissionError()
            validate_settings(guard, settings)
            paths, portable = _resolve_paths(guard, settings, project_root)
        bindings.append(
            Binding(
                entry['id'],
                name,
                guard,
                entry['stage'],
                entry['action'],
                json.loads(json.dumps(settings)),
                entry['text'],
                entry['source']['file'],
                paths,
                portable,
            )
        )
    for name, spec in specs.items():
        mine = [b for b in bindings if b.guard_name == name]
        if not mine or not all(b.bound for b in mine):
            raise RuleAdmissionError('invalidPolicy', f'required guard {name} is not bound to a registered guard')
        if any(STRENGTH[b.action] < STRENGTH[spec['action']] for b in mine):
            raise RuleAdmissionError('invalidPolicy', f'required guard {name}: action weaker than host minimum')
    if warn:
        for notice in notices:
            warnings.warn(
                f'guard {notice["guard"]} bound at {notice["stage"]} in {notice["sources"][0]} and '
                f'{notice["sources"][1]}; keeping the stronger {notice["action"]} binding',
                RuleBindingWarning,
                stacklevel=2,
            )
    return RuleSet(tuple(bindings), tuple(prose), tuple(notices))


def _markdown(path):
    return path.lower().endswith('.md')


def _projection(entry):
    fields = [entry.get(key) for key in ('kind', 'guard', 'stage', 'action', 'settings', 'reason', 'text')]
    return json.dumps(fields, sort_keys=True, default=repr)


def artifact_rules(artifact):
    """Return (entries, issues): inspection rules, cross-checked against Core's parse of the body.

    Hand-built artifacts without inspection rules are parsed with Core's own
    parse_rules. A Core without parse_rules fails closed for anything tag-like.
    """
    api = _core_api()
    texts = [artifact.body] + [r['content'] for r in artifact.resources if _markdown(r['relPath'])]
    supplied = artifact.rules is not None
    if api is None:
        if (supplied and (artifact.rules or artifact.rule_issues)) or any(_LEXICAL_TAG.search(t) for t in texts):
            raise RuleAdmissionError()
        return [], []
    parse_rules, strip_tags = api
    parsed = parse_rules(artifact.body, artifact.id)
    issues = list(parsed['issues'])
    issues += [
        {'code': 'enforce-in-resource'}
        for r in artifact.resources
        if _markdown(r['relPath']) and strip_tags(r['content']) != r['content']
    ]
    if not supplied:
        return parsed['rules'], issues
    entries = list(artifact.rules)
    for entry in entries:
        _entry(entry)
    # Inspection may carry rules from several composed files, but it must
    # never omit a tag present in this body: that would look enforced but isn't.
    known = {_projection(entry) for entry in entries}
    if any(_projection(entry) not in known for entry in parsed['rules']):
        raise RuleAdmissionError()
    # Inspection issues already include the body's; Runtime's own parse is the safety net when they are absent.
    return entries, list(artifact.rule_issues or ()) or issues


def turn_rules(runtime, artifact, *, warn=True):
    """Admit an artifact's rules for a conversation turn; closed codes only.

    The project root for path-like settings is the Prosaic project directory,
    the parent of the runtime's source directory, resolved once when the
    runtime is constructed so a later working-directory change cannot move it.
    """
    try:
        entries, issues = artifact_rules(artifact)
        return compile_rules(
            entries,
            runtime._guards,
            allow_unbound=runtime._allow_unbound_guards,
            required_guards=runtime.config.required_guards,
            project_root=runtime._project_root,
            issues=issues,
            warn=warn,
        )
    except RuleAdmissionError:
        raise
    except Exception as exc:  # Hostile tag content must never crash admission.
        raise RuleAdmissionError() from exc


def refuse_unenforced(artifact):
    """Entry points without rule hooks refuse any enforce tag or tag problem, with a closed ValueError."""
    try:
        entries, issues = artifact_rules(artifact)
    except RuleAdmissionError:
        raise
    except Exception as exc:  # A malformed artifact or parser fault refuses rather than escapes.
        raise RuleAdmissionError() from exc
    if issues or any(entry['kind'] == 'enforce' for entry in entries):
        raise ValueError('enforcement tags require conversation turn admission; this entry point cannot enforce them')
