"""Offline preflight by default; --live opts in to native CLI-tool inference."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from prosaic_runtime import ProsaicRuntime, RuntimeConfig, RunPolicy

EXAMPLES = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=EXAMPLES / 'cli-tools.yml')
    parser.add_argument('--profile', default='qwen')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--no-stream', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = RuntimeConfig.load(args.config)
        endpoint = config.profiles[args.profile]
        if args.no_stream:
            endpoint = replace(endpoint, features={**endpoint.features, 'streaming': False})
        config = replace(config, profiles={**config.profiles, args.profile: endpoint},
                         routes={**config.routes, 'balanced': args.profile})
        runtime = ProsaicRuntime(config, source=EXAMPLES / '.prosaic')
        policy = RunPolicy(allowed_tools=frozenset({'analyze_spec'}), read_roots=('evidence',),
                           initial_tool='analyze_spec', timeout_s=config.limits.timeout_s,
                           max_tool_rounds=config.limits.max_tool_rounds)
        artifact = 'subagents/cli-spec-reviewer.md'
        if not args.live:
            report = runtime.preflight(artifact, cwd=EXAMPLES, policy=policy)
            print(json.dumps(report))
            return 0 if report['ok'] else 1
        events = []
        result = runtime.run(artifact, cwd=EXAMPLES, policy=policy, on_event=events.append)
        observed = any(e.get('event') == 'tool_completed' and e.get('name') == 'analyze_spec'
                       and e.get('status') == 'ok' for e in events)
        print(json.dumps({'result': asdict(result), 'tool_executed': observed, 'events': events}))
        return 0 if result.exit_code == 0 and observed else 1
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
