"""Execute a host-registered synthetic catalogue lookup (requires explicit --live)."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from prosaic_runtime import ProsaicRuntime, RuntimeConfig, RunPolicy
from catalog_tools import make_tools

EXAMPLES = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--config', type=Path, default=EXAMPLES / 'custom-tools.yml')
    parser.add_argument('--profile')
    parser.add_argument('--base-url')
    parser.add_argument('--model')
    parser.add_argument('--sku', default='SKU-001')
    parser.add_argument('--no-stream', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required before endpoint requests')
    try:
        config = RuntimeConfig.load(args.config)
        profile = args.profile or config.routes.get('fast', config.default_profile)
        endpoint = config.profiles[profile]
        overrides = {key: value for key, value in [('base_url', args.base_url), ('model', args.model)] if value is not None}
        if args.no_stream:
            overrides['features'] = {**endpoint.features, 'streaming': False}
        config = replace(config, profiles={**config.profiles, profile: replace(endpoint, **overrides)},
                         routes={**config.routes, 'fast': profile})
        events = []
        runtime = ProsaicRuntime(config, source=EXAMPLES / '.prosaic', custom_tools=make_tools())
        result = runtime.run('subagents/catalog-reader.md', args.sku, cwd=EXAMPLES,
            policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog',
                             timeout_s=config.limits.timeout_s, max_tool_rounds=config.limits.max_tool_rounds),
            on_event=events.append)
        print(json.dumps({'result': asdict(result), 'events': events}))
        return 0 if result.exit_code == 0 else 1
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
