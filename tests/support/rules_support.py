"""Synthetic guards and tagged artifacts for rule admission tests."""

from prosaic import parse_rules

from prosaic_runtime import Guard, ProsaicArtifact, ProsaicRuntime, Verdict

TEXT_STAGES = ('before_answer', 'final')


def tagged(body, *, tools=(), artifact_id='subagents/shelf-keeper.md', resources=(), line_offset=0):
    """Mimic inspection: rules come from Core's own parser, never from Runtime."""
    parsed = parse_rules(body, artifact_id, line_offset)
    document = {
        'id': artifact_id,
        'type': 'subagent',
        'frontmatter': {'tools': sorted(tools)},
        'body': body,
        'resources': list(resources),
    }
    if parsed['rules'] or parsed['issues']:
        document.update(rules=parsed['rules'], ruleIssues=parsed['issues'])
    return ProsaicArtifact.from_inspection(document)


def runtime_for(fx, *, guards=(), allow_unbound_guards=False, required_guards=None):
    from dataclasses import replace

    config = fx.runtime.config
    if required_guards is not None:
        config = replace(config, required_guards=required_guards)
    return ProsaicRuntime(config, custom_tools=fx.tools, guards=list(guards), allow_unbound_guards=allow_unbound_guards)


class Recorder:
    """A guard whose verdicts are scripted per call; records what it saw."""

    def __init__(
        self, name, verdicts=(), *, stages=TEXT_STAGES, version='v1', settings_schema=None, path_settings=(), fixes=True
    ):
        self.verdicts, self.calls = list(verdicts), []
        kwargs = {} if settings_schema is None else {'settings_schema': settings_schema}
        self.guard = Guard(name, version, stages, self.check, fixes=fixes, path_settings=path_settings, **kwargs)

    def check(self, stage, context):
        self.calls.append((stage, context))
        if not self.verdicts:
            return Verdict(True)
        verdict = self.verdicts.pop(0)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict


def recorder(name, *verdicts, **kwargs):
    return Recorder(name, verdicts, **kwargs)
