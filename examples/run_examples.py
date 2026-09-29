"""Run neutral prose with and without tools using the public Python API.

Install prosaic-runtime and the Prosaic CLI first. Credentials come from the
environment variable named in YAML; never put an API key in this script.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import time

from prosaic_runtime import ProsaicRuntime, RuntimeConfig, RunPolicy

EXAMPLES = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="use one YAML config for both examples")
    parser.add_argument("--base-url", help="override endpoint URL in memory")
    parser.add_argument("--model", help="override model ID in memory")
    parser.add_argument("--timeout", type=float, help="override each example's YAML timeout")
    parser.add_argument("--events", action="store_true", help="include runtime events in JSONL output")
    args = parser.parse_args()

    examples = [
        ("summarizer", "prosaic-runtime.yaml",
         "S1: The pilot processed 120 requests. S2: Three requests timed out. "
         "S3: The cause has not been established."),
        ("reviewer", "with-tools.yml",
         "Review evidence/pilot.md. What do we know, and what remains unknown?"),
    ]
    try:
        if args.timeout is not None:
            RunPolicy(timeout_s=args.timeout)  # Validate before making a model call.
        for name, filename, arguments in examples:
            config = RuntimeConfig.load(args.config if args.config else EXAMPLES / filename)
            overrides = {}
            if args.base_url is not None:
                overrides["base_url"] = args.base_url
            if args.model is not None:
                overrides["model"] = args.model
            config = replace(config, profiles={key: replace(endpoint, **overrides)
                                              for key, endpoint in config.profiles.items()})
            reader = name == "reviewer"
            policy = RunPolicy(
                allowed_tools=frozenset({"read_file"}) if reader else frozenset(),
                read_roots=("evidence",) if reader else (),
                timeout_s=args.timeout if args.timeout is not None else config.limits.timeout_s,
                max_tool_rounds=config.limits.max_tool_rounds,
            )

            def on_event(event):
                if args.events:
                    print(json.dumps({"example": name, **event}), flush=True)
                elif event["event"] in {"started", "completed", "tool_started", "tool_completed"}:
                    print(f"[{name}] {event['event']}", file=sys.stderr, flush=True)

            started = time.monotonic()
            runtime = ProsaicRuntime(config, source=EXAMPLES / ".prosaic")
            result = runtime.run(f"subagents/{name}.md", arguments=arguments,
                                 cwd=EXAMPLES, policy=policy, on_event=on_event)
            print(json.dumps({"event": "result", "example": name,
                              "elapsed_s": round(time.monotonic() - started, 2),
                              **asdict(result)}), flush=True)
            if result.exit_code:
                return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
