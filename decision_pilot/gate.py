"""Experiment 3: gate agent tool calls (allow, escalate or block)."""

from __future__ import annotations

from .client import Provider


def questions(config: dict) -> dict:
    return {"unsafe": {"type": "noul", "instructions": config["instructions"], "criteria": config["criteria"]}}


def decide(provider: Provider, config: dict, tool: str, args: dict, context: str) -> dict:
    state = {"tool": tool, "arguments": args, "context": context}
    probability = provider.ask(state, questions(config))["unsafe"]["probability"]
    thresholds = config["thresholds"]
    if probability >= thresholds["block"]:
        action = "block"
    elif probability >= thresholds["escalate"]:
        action = "escalate"
    else:
        action = "allow"
    return {"unsafe": probability, "action": action}


EXIT_CODES = {"allow": 0, "escalate": 2, "block": 3}
