"""Small CLI; stdout is reserved for the requested machine output."""
import argparse
from dataclasses import asdict
import json
import subprocess
import sys
from .runtime import ProsaicRuntime
from .policy import RunPolicy
from .config import RuntimeConfig
from .console import Progress
from .diagnostics import doctor, smoke, failure_hint


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv.pop(0) if argv and argv[0] in {"doctor", "smoke"} else "run"
    parser = argparse.ArgumentParser(prog="prosaic-runtime")
    if mode == "run":
        parser.add_argument("artifact", help="Prosaic artifact identifier (also: doctor or smoke subcommands)")
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
    args = parser.parse_args(argv)
    def emit(event):
        print(json.dumps(event), flush=True)
    try:
        config = RuntimeConfig.load(args.config)
        policy = RunPolicy(frozenset(args.allow_tool), tuple(args.read_root), tuple(args.write_path),
                           tuple(args.forbid_root),
                           args.timeout if args.timeout is not None else config.limits.timeout_s,
                           args.max_tool_rounds if args.max_tool_rounds is not None else config.limits.max_tool_rounds)
        with Progress(args.quiet or args.events) as progress:
            sink = emit if args.events else progress
            if mode != "run":
                profile = args.profile or config.default_profile
                if profile not in config.profiles:
                    raise ValueError(f"Unknown endpoint profile: {profile}")
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
            runtime = ProsaicRuntime(config, source=args.source)
            result = runtime.run(args.artifact, args.arguments, cwd=args.cwd, policy=policy, on_event=sink)
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
