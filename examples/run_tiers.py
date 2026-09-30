"""Run four independent Markdown agents against a synthetic launch dossier.

Real model requests; no shell, recursive agents, or writes. Tier selection stays
in Prosaic frontmatter and model mapping stays in YAML. This is not a benchmark.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from prosaic_runtime import ProsaicRuntime, RuntimeConfig, RunPolicy

EXAMPLES = Path(__file__).resolve().parent
DOSSIER = EXAMPLES / "evidence/launch"
AGENTS = {
    "fast": "launch-briefer",
    "balanced": "launch-analyst",
    "strong": "launch-skeptic",
    "ultra": "launch-decision",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=EXAMPLES / "tokenproxy.yml")
    parser.add_argument("--tier", choices=["all", *AGENTS], default="all")
    parser.add_argument("--timeout", type=float, help="per-agent override in seconds")
    parser.add_argument("--events", action="store_true", help="JSONL events/results instead of readable answers")
    args = parser.parse_args(argv)
    try:
        config = RuntimeConfig.load(args.config)
        timeout = args.timeout if args.timeout is not None else config.limits.timeout_s
        RunPolicy(timeout_s=timeout)  # Reject bad limits before any network request.
        tiers = list(AGENTS) if args.tier == "all" else [args.tier]
        if any(tier not in config.routes for tier in tiers):
            raise ValueError("configuration needs routes for every selected tier")
        if any(tier in {"balanced", "strong"} for tier in tiers) and "read_file" not in config.allowed_tools:
            raise ValueError("tool examples require read_file in configuration allowed_tools")
        runtime = ProsaicRuntime(config, source=EXAMPLES / ".prosaic")
        for tier in tiers:
            reader = tier in {"balanced", "strong"}
            files = [DOSSIER / "brief.md"] if tier == "fast" else sorted(DOSSIER.glob("*.md"))
            arguments = ("Read evidence/launch/brief.md, evidence/launch/metrics.md, and "
                         "evidence/launch/field-notes.md. Assess the proposed Harbor launch.")
            if not reader:
                arguments = "\n\n".join(f"## Source: evidence/launch/{path.name}\n{path.read_text(encoding='utf-8')}"
                                          for path in files)
            policy = RunPolicy(allowed_tools=frozenset({"read_file"}) if reader else frozenset(),
                               read_roots=("evidence/launch",) if reader else (),
                               timeout_s=timeout, max_tool_rounds=config.limits.max_tool_rounds)
            observed = {"streaming": False, "tool_execution": False}
            selected = {}

            def on_event(event):
                kind = event["event"]
                if kind == "started":
                    selected.update(model=event["model"], profile=event["profile"])
                if kind == "text_delta":
                    observed["streaming"] = True
                if kind == "tool_completed" and event["name"] == "read_file" and event["status"] == "ok":
                    observed["tool_execution"] = True
                if args.events:
                    print(json.dumps({**event, "tier": tier}), flush=True)
                elif kind in {"started", "completed", "tool_started", "tool_completed"}:
                    detail = (f" {event['model']} ({event['profile']})" if kind == "started" else
                              f" {event['name']} {event.get('status', '')}" if "name" in event else "")
                    print(f"[{tier}] {kind}{detail}", file=sys.stderr, flush=True)

            started = time.monotonic()
            result = runtime.run(f"subagents/{AGENTS[tier]}.md", arguments=arguments,
                                 cwd=EXAMPLES, policy=policy, on_event=on_event)
            ok = result.exit_code == 0 and bool(result.stdout.strip()) and (not reader or observed["tool_execution"])
            row = {**asdict(result), "event": "result", "tier": tier, **selected, **observed,
                   "elapsed_s": round(time.monotonic() - started, 2), "demo_ok": ok}
            if args.events:
                print(json.dumps(row), flush=True)
            else:
                print(f"\n=== {tier}: {selected.get('model', 'unknown')} ===\n{result.stdout}")
                print(f"\nElapsed: {row['elapsed_s']}s | Tokens: {result.token_usage} | "
                      f"Streaming: {observed['streaming']} | Read tool: {observed['tool_execution']} | Passed: {ok}", flush=True)
            if not ok:
                print(result.stderr or "Demo failed: expected a nonempty completion and, for tool agents, a successful read_file.",
                      file=sys.stderr, flush=True)
                return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
