"""Small CLI; stdout is reserved for the requested machine output."""
import argparse
from dataclasses import asdict
import json
import subprocess
import sys
from .runtime import ProsaicRuntime
from .policy import RunPolicy


def main():
    parser = argparse.ArgumentParser(prog="prosaic-runtime")
    parser.add_argument("artifact", help="Prosaic command or subagent identifier")
    parser.add_argument("--source", default=".prosaic")
    parser.add_argument("--config", help="YAML configuration (default: prosaic-runtime.yaml, then prosaic-runtime.yml)")
    parser.add_argument("--arguments", default="")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--allow-tool", action="append", default=[])
    parser.add_argument("--read-root", action="append", default=[])
    parser.add_argument("--write-path", action="append", default=[])
    parser.add_argument("--forbid-root", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tool-rounds", type=int, default=8)
    parser.add_argument("--events", action="store_true", help="emit JSONL events followed by result")
    args = parser.parse_args()
    def emit(event):
        print(json.dumps(event), flush=True)
    try:
        runtime = ProsaicRuntime.from_config(args.config, source=args.source)
        policy = RunPolicy(frozenset(args.allow_tool), tuple(args.read_root), tuple(args.write_path),
                           tuple(args.forbid_root), args.timeout, args.max_tool_rounds)
        result = runtime.run(args.artifact, args.arguments, cwd=args.cwd, policy=policy,
                             on_event=emit if args.events else None)
        emit({"event": "result", **asdict(result)})
        return 0 if result.exit_code == 0 else 1
    except KeyboardInterrupt:
        return 130
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
