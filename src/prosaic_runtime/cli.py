"""Small CLI; stdout is reserved for the requested machine output."""
import argparse
from dataclasses import asdict
import json
import os
import subprocess
import sys
from .runtime import ProsaicRuntime
from .policy import RunPolicy
from .config import RuntimeConfig
from .console import Progress
from .diagnostics import doctor, smoke, failure_hint, conformance
from .accounting import ExecutionContext, RateCard


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv.pop(0) if argv and argv[0] in {"doctor", "smoke", "preflight", "conformance"} else "run"
    parser = argparse.ArgumentParser(prog="prosaic-runtime")
    if mode == "run":
        parser.add_argument("artifact", help="Prosaic artifact identifier (also: doctor or smoke subcommands)")
    if mode == 'preflight':
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--agent', help='Prosaic artifact identifier to check without inference')
        target.add_argument('--all-tools', action='store_true', help='check every registered/configured tool')
    if mode == 'run':
        parser.add_argument('--initial-tool', help='require this granted tool as the first native call')
        for name in ('application_id', 'tenant_id', 'billing_account_id', 'actor_id', 'project_id', 'request_id', 'run_id', 'invocation_id', 'parent_invocation_id'):
            parser.add_argument('--' + name.replace('_', '-'), help='trusted execution attribution; omitted IDs use defaults')
        parser.add_argument('--accounting-dsn-env', help='environment variable holding the accounting PostgreSQL DSN; requires separately installed adapter')
        parser.add_argument('--accounting-namespace', help='persistent deployment namespace')
        parser.add_argument('--accounting-environment', help='isolated environment, e.g. production or sandbox')
        parser.add_argument('--accounting-rate-card', help='explicit JSON rate card for internal estimates')
        parser.add_argument('--accounting-provider', choices=('openai-compatible', 'anthropic'),
                            help='accounting provider identity; defaults to openai-compatible and must match the selected endpoint')
    parser.add_argument("--source", default=".prosaic")
    parser.add_argument("--config", help="YAML configuration (default: prosaic-runtime.yaml, then prosaic-runtime.yml)")
    parser.add_argument("--arguments", default="")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--allow-tool", action="append", default=[])
    parser.add_argument("--read-root", action="append", default=[])
    parser.add_argument("--write-path", action="append", default=[])
    parser.add_argument("--forbid-root", action="append", default=[])
    parser.add_argument("--timeout", type=float, help="override YAML limits.timeout_s")
    parser.add_argument("--max-tool-rounds", type=int, help="override YAML limits.max_tool_rounds")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--events", action="store_true", help="emit JSONL events followed by result")
    output.add_argument("--output", choices=("json", "text"), default="json")
    parser.add_argument("--quiet", action="store_true", help="suppress stderr progress (errors remain visible)")
    if mode != "run":
        parser.add_argument("--profile", help="endpoint profile; defaults to default_profile")
    if mode == "doctor":
        parser.add_argument("--inference", action="store_true", help="opt in to one model request (may incur charges)")
    if mode == "smoke":
        parser.add_argument("--live", action="store_true", required=True, help="explicitly authorize live model calls")
    if mode == 'conformance':
        parser.add_argument('--live', action='store_true', help='explicitly authorize bounded provider calls')
    args = parser.parse_args(argv)
    def emit(event):
        print(json.dumps(event), flush=True)
    try:
        config = RuntimeConfig.load(args.config)
        policy = RunPolicy(frozenset(args.allow_tool), tuple(args.read_root), tuple(args.write_path),
                           tuple(args.forbid_root),
                           args.timeout if args.timeout is not None else config.limits.timeout_s,
                           args.max_tool_rounds if args.max_tool_rounds is not None else config.limits.max_tool_rounds,
                           initial_tool=getattr(args, 'initial_tool', None))
        with Progress(args.quiet or args.events) as progress:
            sink = emit if args.events else progress
            if mode == 'preflight':
                runtime = ProsaicRuntime(config, source=args.source)
                report = runtime.preflight(args.agent, all_tools=args.all_tools, cwd=args.cwd, policy=policy)
                if args.output == 'text':
                    print(f"preflight: {'passed' if report['ok'] else 'failed'}")
                    for name, check in report['checks'].items():
                        print(f"{name}: {check['status']} — {check['message']}")
                else:
                    emit(report)
                return 0 if report['ok'] else 1
            if mode != "run":
                profile = args.profile or config.default_profile
                if profile not in config.profiles:
                    raise ValueError(f"Unknown endpoint profile: {profile}")
                if mode == 'conformance':
                    suite_policy = RunPolicy(timeout_s=args.timeout if args.timeout is not None else 60,
                        max_tool_rounds=args.max_tool_rounds if args.max_tool_rounds is not None else 2,
                        max_input_bytes=65536, max_provider_requests=12, max_tool_calls=4,
                        max_reported_tokens=32768)
                    report = conformance(config, profile, live=args.live, policy=suite_policy,
                                         observer=emit if args.events else None)
                    if args.output == 'text':
                        print('conformance: ' + report['qualification'])
                        for case in report['cases']:
                            print(f"{case['id']}: {case['state']} — {case['reason']}")
                    else:
                        emit(report)
                    return 0 if not args.live or report['qualification'] == 'qualified' else 1
                if mode == "doctor":
                    report = doctor(config, profile, inference=args.inference, timeout_s=policy.timeout_s, on_event=sink)
                else:
                    report = smoke(config, profile, timeout_s=policy.timeout_s,
                                   max_tool_rounds=policy.max_tool_rounds, on_event=sink)
                if args.output == "text":
                    print(f"{mode}: {'passed' if report['ok'] else 'failed'}")
                    for name, check in report.get("checks", report.get("tests", {})).items():
                        print(f"{name}: {check['status']}" + (f" — {check['message']}" if check.get("message") else ""))
                else:
                    emit(report)
                return 0 if report["ok"] else 1
            recorder = None
            if args.accounting_dsn_env:
                if not args.accounting_namespace or not args.accounting_environment:
                    raise ValueError('accounting requires explicit namespace and environment')
                dsn = os.environ.get(args.accounting_dsn_env)
                if not dsn:
                    raise ValueError('accounting DSN environment variable is not set')
                try:
                    from prosaic_runtime_postgres import PostgresRecorder
                except ImportError as exc:
                    raise ValueError('install b3-prosaic-runtime-postgres to enable durable accounting') from exc
                rate_card = None
                if args.accounting_rate_card:
                    with open(args.accounting_rate_card, encoding='utf-8') as handle:
                        rate_card = RateCard(**json.load(handle))
                recorder = PostgresRecorder(dsn, namespace=args.accounting_namespace,
                    environment=args.accounting_environment, rate_card=rate_card,
                    provider_id=args.accounting_provider or 'openai-compatible')
            elif args.accounting_namespace or args.accounting_environment or args.accounting_rate_card or args.accounting_provider:
                raise ValueError('accounting options require --accounting-dsn-env')
            names = ('application_id', 'tenant_id', 'billing_account_id', 'actor_id', 'project_id', 'request_id', 'run_id', 'invocation_id', 'parent_invocation_id')
            values = {name: getattr(args, name) for name in names if getattr(args, name) is not None}
            runtime = ProsaicRuntime(config, source=args.source, accounting=recorder)
            result = runtime.run(args.artifact, args.arguments, cwd=args.cwd, policy=policy, on_event=sink,
                                 **({'context': ExecutionContext(**values)} if values else {}))
        if args.output == "text":
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.exit_code:
                print(failure_hint(result), file=sys.stderr)
            return 0 if result.exit_code == 0 else 1
        if result.exit_code:
            print(failure_hint(result), file=sys.stderr)
        emit({"event": "result", **asdict(result)})
        return 0 if result.exit_code == 0 else 1
    except KeyboardInterrupt:
        return 130
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
