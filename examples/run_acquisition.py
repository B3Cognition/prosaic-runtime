"""Run opt-in staged acquisition and explicit no-tool preloading through Prosaic.

This demonstrates transport, not factual admission. For schema/source checks use
the companion Prosaic Harness evidence blueprints. No fallback is automatic.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

from prosaic_runtime import ProsaicRuntime, RuntimeConfig, RunPolicy

EXAMPLES = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=EXAMPLES / 'with-tools.yml')
    parser.add_argument('--profile', help='route both examples to this configured profile')
    parser.add_argument('--base-url')
    parser.add_argument('--model')
    parser.add_argument('--no-stream', action='store_true')
    parser.add_argument('--mode', choices=['staged', 'preloaded', 'both'], default='both')
    args = parser.parse_args()
    config = RuntimeConfig.load(args.config)
    profile = args.profile or config.default_profile
    if profile not in config.profiles:
        parser.error('unknown profile')
    endpoint = config.profiles[profile]
    endpoint = replace(endpoint, base_url=args.base_url or endpoint.base_url, model=args.model or endpoint.model,
                       features={**endpoint.features, 'streaming': not args.no_stream})
    config = replace(config, profiles={**config.profiles, profile: endpoint}, routes={**config.routes, 'fast': profile})
    runtime = ProsaicRuntime(config, source=EXAMPLES / '.prosaic')
    modes = ['staged', 'preloaded'] if args.mode == 'both' else [args.mode]
    for mode in modes:
        if mode == 'staged':
            result = runtime.run('subagents/acquired-reviewer.md', 'Distinguish observations from unknowns.',
                acquisition='subagents/acquire-pilot.md', cwd=EXAMPLES,
                policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('evidence/pilot.md',),
                    initial_tool='read_file', timeout_s=config.limits.timeout_s, max_tool_rounds=config.limits.max_tool_rounds))
        else:
            # An explicit host read of a fixed example asset, not a model tool.
            evidence = (EXAMPLES / 'evidence/pilot.md').read_text()
            result = runtime.run('subagents/preloaded-reviewer.md', evidence, cwd=EXAMPLES)
        print(json.dumps({'mode': mode, **asdict(result)}), flush=True)
        if result.exit_code:
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
