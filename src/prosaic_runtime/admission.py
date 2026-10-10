"""Pure execution-metadata checks for already inspected Prosaic artifacts."""
from .artifacts import ProsaicArtifact
from .config import EndpointConfig, RuntimeConfig
from .policy import requested_tools
from .anthropic import validate_anthropic_controls


def _inspection_artifact(artifact):
    if not isinstance(artifact, ProsaicArtifact):
        raise ValueError('execution admission requires a Prosaic inspection artifact')
    try:
        return ProsaicArtifact.from_inspection({
            'id': artifact.id, 'type': artifact.type, 'frontmatter': artifact.frontmatter,
            'body': artifact.body, 'resources': list(artifact.resources)})
    except (TypeError, AttributeError) as exc:
        raise ValueError('invalid Prosaic inspection artifact') from exc


def validate_execution_artifact(artifact, config, *, acquisition=None) -> None:
    """Check structural and supported execution metadata without preparing a run.

    Inputs must already be inspected artifacts and a RuntimeConfig. This function
    does not inspect paths, discover tools, render prompts, construct a Runtime or
    invoke callbacks/transports. Tool registration/grants and invocation-dependent
    limits remain separate checks. Canonical prose fields belong to Prosaic.
    """
    artifact = _inspection_artifact(artifact)
    if not isinstance(config, RuntimeConfig):
        raise ValueError('execution admission requires a RuntimeConfig')
    requested = requested_tools(artifact.frontmatter.get('tools'))
    effort = artifact.frontmatter.get('effort')
    if effort is not None and (not isinstance(effort, str) or effort not in {'low', 'medium', 'high'}):
        raise ValueError(f'unsupported effort: {effort}')
    tier = artifact.frontmatter.get('model_tier')
    try:
        if tier is not None and (not isinstance(tier, str) or tier not in config.routes):
            raise ValueError(f'no endpoint route for model_tier: {tier}')
        profile = config.routes[tier] if tier is not None else config.default_profile
        endpoint = config.profiles[profile]
        if not isinstance(endpoint, EndpointConfig):
            raise ValueError('selected profile requires an EndpointConfig')
        # Runtime disables these controls regardless of operator configuration.
        features = {**endpoint.features, 'web_tools': False, 'transcript': False}
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError('invalid selected runtime profile or route') from exc
    if endpoint.provider == 'anthropic':
        validate_anthropic_controls(features, {'effort': effort})
    if acquisition is not None:
        acquisition = _inspection_artifact(acquisition)
        acquisition_tools = requested_tools(acquisition.frontmatter.get('tools'))
        if acquisition_tools - requested:
            raise ValueError('acquisition tools cannot broaden final prose tools')
        for key in ('model_tier', 'effort'):
            if key in acquisition.frontmatter and acquisition.frontmatter[key] != artifact.frontmatter.get(key):
                raise ValueError(f'acquisition {key} must match final prose or be omitted')
        # Acquisitions use the selected final endpoint and effort, never a new
        # route/default selection based on their omitted metadata.
