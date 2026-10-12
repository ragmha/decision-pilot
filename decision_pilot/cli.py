"""Command line: route one issue, gate one tool call, or replay labelled history."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import gate, routing
from .client import DecisionError, provider_from_env
from .log import record
from .replay import replay_gate, replay_routing

ROOT = Path(__file__).resolve().parent.parent


def _config(name: str, path: str | None) -> dict:
    return json.loads(Path(path or ROOT / "config" / f"{name}.json").read_text())


def _summary(path: str | None, lines: list[str]) -> None:
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision-pilot")
    parser.add_argument("--config", help="override the decision config file")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("route", help="route one GitHub issue")
    r.add_argument("--title", required=True)
    r.add_argument("--body", default="")
    r.add_argument("--id", default="adhoc")
    r.add_argument("--log", help="append the decision to this JSONL file")
    r.add_argument("--summary", help="append a Markdown summary (defaults to $GITHUB_STEP_SUMMARY)")

    g = sub.add_parser("gate", help="decide whether an agent tool call may run")
    g.add_argument("--tool", required=True)
    g.add_argument("--args", default="{}", help="tool arguments as JSON")
    g.add_argument("--context", default="")
    g.add_argument("--id", default="adhoc")
    g.add_argument("--log")

    p = sub.add_parser("replay", help="replay labelled history and write a report")
    p.add_argument("pilot", choices=["routing", "gate"])
    p.add_argument("--data", help="labelled JSONL (defaults to data/)")
    p.add_argument("--out", default="reports")

    args = parser.parse_args(argv)
    try:
        provider = provider_from_env()
        if args.command == "route":
            config = _config("routing", args.config)
            start = time.perf_counter()
            decision = routing.decide(provider, config, args.title, args.body)
            elapsed = (time.perf_counter() - start) * 1000
            record(Path(args.log) if args.log else None, "routing", args.id, provider,
                   config["version"], decision, elapsed)
            print(json.dumps(decision))
            _summary(args.summary, [
                "### Decision pilot: issue routing", "",
                f"Provider `{provider.name}` · config `{config['version']}`", "",
                "| Team | Confidence | Severity | Action |", "| --- | --- | --- | --- |",
                f"| {decision['team']} | {decision['confidence']:.0%} | {decision['severity_label']} "
                f"| {decision['action']} |",
            ])
            return 0
        if args.command == "gate":
            config = _config("gate", args.config)
            start = time.perf_counter()
            decision = gate.decide(provider, config, args.tool, json.loads(args.args), args.context)
            record(Path(args.log) if args.log else None, "gate", args.id, provider, config["version"],
                   decision, (time.perf_counter() - start) * 1000)
            print(json.dumps(decision))
            return gate.EXIT_CODES[decision["action"]]
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        config = _config(args.pilot, args.config)
        data = Path(args.data or ROOT / "data" / ("issues.jsonl" if args.pilot == "routing" else "tool_calls.jsonl"))
        result = (replay_routing if args.pilot == "routing" else replay_gate)(provider, config, data, out)
        print(json.dumps(result))
        print(f"report: {out / (args.pilot + '.md')}")
        return 0
    except DecisionError as error:
        parser.exit(1, f"decision-pilot: {error}\n")
