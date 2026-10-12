"""Experiment 2: route GitHub issues before anyone reads them."""

from __future__ import annotations

from .client import Provider


def questions(config: dict) -> dict:
    return {
        "team": {"type": "choice", "instructions": config["instructions"], "criteria": config["teams"]},
        "severity": {"type": "score", "instructions": "How severe is this issue?",
                     "criteria": config["severity"]},
    }


def decide(provider: Provider, config: dict, title: str, body: str) -> dict:
    answers = provider.ask({"title": title, "body": body}, questions(config))
    team = answers["team"]
    confidence = team["probabilities"][team["choice"]]
    thresholds = config["thresholds"]
    if confidence >= thresholds["act"]:
        action = "act"
    elif confidence >= thresholds["review"]:
        action = "review"
    else:
        action = "triage"
    levels = len(config["severity"])
    severity = min(levels - 1, max(0, round(answers["severity"]["score"])))
    return {
        "team": team["choice"],
        "confidence": confidence,
        "probabilities": team["probabilities"],
        "severity": severity,
        "severity_label": config["severity"][severity].split(":")[0],
        "action": action,
    }
