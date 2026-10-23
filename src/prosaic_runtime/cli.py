"""Small CLI; stdout is reserved for the requested machine output."""
import argparse
from dataclasses import asdict, replace
import json
import os
import subprocess
import sys
from pathlib import Path
import yaml
from .runtime import ProsaicRuntime
from .policy import RunPolicy
from .config import RuntimeConfig, CONFIG_FILENAMES
from .native import NativeRuntime
from .native_config import NativeRuntimeConfig, NativePolicy
from .console import Progress
from .diagnostics import doctor, smoke, failure_hint, conformance
from .accounting import ExecutionContext, RateCard
from .unified import UnifiedRuntime
from .unified_config import UnifiedRuntimeConfig, PoolPolicy


def _pool_policy(args):
    """Construct host authority without inheriting backend configuration defaults."""
    for name in ('timeout', 'max_attempts', 'max_native_attempts'):
        if getattr(args, name) is None:
            raise ValueError('UnifiedRuntime requires --' + name.replace('_', '-'))
    if args.profile is not None or any(getattr(args, name) is not None for name in (
            'application_id', 'tenant_id', 'billing_account_id', 'actor_id', 'project_id',
            'request_id', 'run_id', 'invocation_id', 'parent_invocation_id',
            'accounting_dsn_env', 'accounting_namespace', 'accounting_environment',
            'accounting_rate_card', 'accounting_provider')):
        raise ValueError('unified execution does not support --profile or accounting/context options')
    native_policy = None
    if args.native_permissions is not None:
        if args.native_timeout is None:
            raise ValueError('native execution in unified pools requires --native-timeout')
        native_policy = NativePolicy(permission_mode=args.native_permissions, timeout_s=args.native_timeout)
    elif args.native_timeout is not None or args.max_native_attempts != 0:
        raise ValueError('native execution requires --native-permissions and --native-timeout; use --max-native-attempts 0 to exclude it')
    api_policy = None
    if args.enable_api:
        for name in ('api_timeout', 'max_api_requests', 'max_api_tool_calls', 'max_tool_rounds'):
            if getattr(args, name) is None:
                raise ValueError('--enable-api requires --' + name.replace('_', '-'))
        api_policy = RunPolicy(allowed_tools=frozenset(args.allow_tool),
            read_roots=tuple(args.read_root), write_paths=tuple(args.write_path),
            forbidden_roots=tuple(args.forbid_root), timeout_s=args.api_timeout,
            max_tool_rounds=args.max_tool_rounds, initial_tool=args.initial_tool,
            max_provider_requests=args.max_api_requests, max_tool_calls=args.max_api_tool_calls,
            max_reported_tokens=args.max_api_reported_tokens)
    elif (args.allow_tool or args.read_root or args.write_path or args.forbid_root or args.initial_tool
            or any(getattr(args, name) is not None for name in ('api_timeout', 'max_api_requests',
                'max_api_tool_calls', 'max_api_reported_tokens', 'max_tool_rounds'))):
        raise ValueError('API budgets and grants require --enable-api')
    if native_policy is None and api_policy is None:
        raise ValueError('enable at least one backend with --native-permissions or --enable-api')
    return PoolPolicy(args.timeout, args.max_attempts, args.max_native_attempts,
                      native_policy, api_policy, frozenset(args.require_capability))


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
    parser.add_argument("--profile", help="explicit execution profile")
    if mode == 'run':
        parser.add_argument('--pool', help='native or unified execution profile pool')
        parser.add_argument('--native-permissions', choices=('read-only', 'workspace-write'),
                            help='explicitly accept vendor-owned native workspace permissions')
        parser.add_argument('--max-attempts', type=int, help='unified pool total dispatch limit (required)')
        parser.add_argument('--max-native-attempts', type=int, help='unified native dispatch limit; explicit zero excludes native')
        parser.add_argument('--native-timeout', type=float, help='unified native per-attempt deadline in seconds')
        parser.add_argument('--enable-api', action='store_true', help='explicitly authorize API members in a unified pool')
        parser.add_argument('--api-timeout', type=float, help='unified API per-attempt deadline in seconds')
        parser.add_argument('--max-api-requests', type=int, help='shared unified API provider-request limit')
        parser.add_argument('--max-api-tool-calls', type=int, help='shared unified API tool-call limit')
        parser.add_argument('--max-api-reported-tokens', type=int, help='optional reported-token cap for API members only')
        parser.add_argument('--require-capability', action='append', default=[], help='required backend capability in a unified pool')
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
        config_path = Path(args.config) if args.config else next((Path(p) for p in CONFIG_FILENAMES if Path(p).exists()), None)
        native = False
        if config_path is not None:
            raw = yaml.safe_load(config_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and raw.get('kind') == 'unified':
                if mode != 'run':
                    raise ValueError('unified configurations currently support execution only')
                policy = _pool_policy(args)
                config = UnifiedRuntimeConfig.load(config_path)
                with Progress(args.quiet or args.events) as progress:
                    result = UnifiedRuntime(config, source=args.source).run(args.artifact, args.arguments,
                        cwd=args.cwd, policy=policy, pool=args.pool, on_event=emit if args.events else progress)
                if args.output == 'text':
                    if result.stdout:
                        print(result.stdout, end='' if result.stdout.endswith('\n') else '\n')
                else:
                    emit({'event': 'result', **asdict(result)})
                if result.exit_code:
                    print(result.metadata.get('failure_reason', 'unified execution failed'), file=sys.stderr)
                return 0 if result.exit_code == 0 else 1
            native = isinstance(raw, dict) and raw.get('kind') == 'native'
        if mode == 'run' and (args.enable_api or args.require_capability or any(
                getattr(args, name) is not None for name in ('max_attempts', 'max_native_attempts',
                'native_timeout', 'api_timeout', 'max_api_requests', 'max_api_tool_calls', 'max_api_reported_tokens'))):
            raise ValueError('unified pool flags require kind: unified configuration')
        if native:
            if mode != 'run':
                raise ValueError('native configurations currently support execution only')
            if args.native_permissions is None:
                raise ValueError('native execution requires --native-permissions')
            if (args.allow_tool or args.read_root or args.write_path or args.forbid_root or args.initial_tool
                    or args.max_tool_rounds is not None or args.accounting_dsn_env
                    or args.accounting_namespace or args.accounting_environment or args.accounting_rate_card
                    or args.accounting_provider or any(getattr(args, name) is not None for name in (
                        'application_id', 'tenant_id', 'billing_account_id', 'actor_id', 'project_id',
                        'request_id', 'run_id', 'invocation_id', 'parent_invocation_id'))):
                raise ValueError('API tool grants, accounting and round limits are unsupported in native mode')
            config = NativeRuntimeConfig.load(config_path)
            native_policy = NativePolicy(permission_mode=args.native_permissions,
                **({'timeout_s': args.timeout} if args.timeout is not None else {}))
            with Progress(args.quiet or args.events) as progress:
                result = NativeRuntime(config, source=args.source).run(args.artifact, args.arguments,
                    cwd=args.cwd, policy=native_policy, profile=args.profile, pool=args.pool,
                    on_event=emit if args.events else progress)
            if args.output == 'text':
                if result.stdout:
                    print(result.stdout)
            else:
                emit({'event': 'result', **asdict(result)})
            if result.exit_code:
                print(result.metadata.get('failure_reason', 'native execution failed'), file=sys.stderr)
            return 0 if result.exit_code == 0 else 1
        if mode == 'run' and (args.pool is not None or args.native_permissions is not None):
            raise ValueError('--pool and --native-permissions require kind: native configuration')
        config = RuntimeConfig.load(args.config)
        if mode == 'run' and args.profile is not None:
            if args.profile not in config.profiles:
                raise ValueError('unknown execution profile')
            config = replace(config, default_profile=args.profile,
                             routes={tier: args.profile for tier in config.routes})
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
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired, yaml.YAMLError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
