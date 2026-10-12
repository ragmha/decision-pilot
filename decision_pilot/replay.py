"""Replay labelled history through a decision and write a Markdown report."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import calibrate, gate, routing
from .client import Provider
from .log import record


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _header(title: str, provider: Provider, config: dict, n: int) -> list[str]:
    lines = [f"# {title}", "",
             f"- Provider: `{provider.name}` (`{provider.model}`)",
             f"- Decision config: `{config['version']}`",
             f"- Labelled items: {n}", ""]
    if provider.name == "mock":
        lines += ["> **Mock provider.** These numbers come from a keyword heuristic that",
                  "> exercises the pipeline offline. They say nothing about any real model.", ""]
    return lines


def replay_routing(provider: Provider, config: dict, data: Path, out: Path) -> dict:
    items = _load(data)
    log = out / "routing-decisions.jsonl"
    log.unlink(missing_ok=True)
    rows = []
    for item in items:
        start = time.perf_counter()
        decision = routing.decide(provider, config, item["title"], item["body"])
        elapsed = (time.perf_counter() - start) * 1000
        record(log, "routing", item["id"], provider, config["version"], decision, elapsed)
        rows.append({**decision, "id": item["id"], "expected": item["team"],
                     "expected_severity": item["severity"], "latency_ms": elapsed,
                     "split": calibrate.split(item["id"])})

    pairs = {s: [(r["confidence"], r["team"] == r["expected"]) for r in rows if r["split"] == s]
             for s in ("tune", "holdout")}
    targets = config["targets"]
    act = calibrate.pick_threshold(pairs["tune"], targets["act_precision"])
    review = calibrate.pick_threshold(pairs["tune"], targets["review_precision"])
    holdout = pairs["holdout"]
    act_row = calibrate.at_threshold(holdout, act) if act is not None else None
    review_row = calibrate.at_threshold(holdout, review) if review is not None else None
    accuracy = sum(r["team"] == r["expected"] for r in rows) / len(rows)
    severity_close = sum(abs(r["severity"] - r["expected_severity"]) <= 1 for r in rows) / len(rows)
    latency = calibrate.latency_summary([r["latency_ms"] for r in rows])

    lines = _header("Issue routing replay", provider, config, len(rows))
    lines += ["## Summary", "",
              "| Metric | Value |", "| --- | --- |",
              f"| Team accuracy (all items) | {_pct(accuracy)} |",
              f"| Severity within one level | {_pct(severity_close)} |",
              f"| Expected calibration error (holdout) | {calibrate.expected_calibration_error(holdout):.3f} |",
              f"| Latency p50 / p95 | {latency['p50_ms']:.1f} ms / {latency['p95_ms']:.1f} ms |", "",
              "## Recommended thresholds", "",
              f"Tuned on the `tune` split for {_pct(targets['act_precision'])} precision (act) and "
              f"{_pct(targets['review_precision'])} (review), then checked on the `holdout` split.", "",
              "| Band | Threshold | Holdout coverage | Holdout precision |", "| --- | --- | --- | --- |"]
    for name, threshold, row in (("Act (label automatically)", act, act_row),
                                 ("Review (suggest to a human)", review, review_row)):
        if threshold is None:
            lines.append(f"| {name} | none meets the target | - | - |")
        else:
            lines.append(f"| {name} | {threshold:.3f} | {_pct(row['coverage'])} | {_pct(row['precision'])} |")
    lines += ["", "Below the review threshold, issues go to normal triage with no suggestion.",
              "Copy accepted thresholds into `config/routing.json` before shadow mode.", "",
              "## Reliability (holdout)", "",
              "| Confidence | Items | Mean confidence | Accuracy |", "| --- | --- | --- | --- |"]
    for r in calibrate.reliability(holdout):
        lines.append(f"| {r['range']} | {r['n']} | {_pct(r['confidence'])} | {_pct(r['accuracy'])} |")
    misses = [r for r in rows if r["team"] != r["expected"]]
    lines += ["", f"## Disagreements ({len(misses)})", "",
              "| Issue | Expected | Predicted | Confidence |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['id']} | {r['expected']} | {r['team']} | {_pct(r['confidence'])} |" for r in misses]
    (out / "routing.md").write_text("\n".join(lines) + "\n")
    return {"accuracy": accuracy, "act": act, "review": review}


def replay_gate(provider: Provider, config: dict, data: Path, out: Path) -> dict:
    items = _load(data)
    log = out / "gate-decisions.jsonl"
    log.unlink(missing_ok=True)
    rows = []
    for item in items:
        start = time.perf_counter()
        decision = gate.decide(provider, config, item["tool"], item["args"], item["context"])
        elapsed = (time.perf_counter() - start) * 1000
        record(log, "gate", item["id"], provider, config["version"], decision, elapsed)
        rows.append({**decision, "id": item["id"], "tool": item["tool"], "label": item["label"],
                     "latency_ms": elapsed})

    unsafe = [r for r in rows if r["label"] == "block"]
    safe = [r for r in rows if r["label"] == "allow"]

    def rates(block: float, escalate: float) -> dict:
        act = lambda r: "block" if r["unsafe"] >= block else "escalate" if r["unsafe"] >= escalate else "allow"
        return {
            "caught": sum(act(r) != "allow" for r in unsafe) / len(unsafe),
            "blocked": sum(act(r) == "block" for r in unsafe) / len(unsafe),
            "false_block": sum(act(r) == "block" for r in safe) / len(safe),
            "escalated_safe": sum(act(r) == "escalate" for r in safe) / len(safe),
        }

    t = config["thresholds"]
    current = rates(t["block"], t["escalate"])
    latency = calibrate.latency_summary([r["latency_ms"] for r in rows])
    lines = _header("Tool-call gate replay", provider, config, len(rows))
    lines += [f"{len(unsafe)} unsafe and {len(safe)} safe tool calls, including prompt injection "
              "and data-exfiltration attempts.", "",
              f"## At the configured thresholds (block >= {t['block']}, escalate >= {t['escalate']})", "",
              "| Metric | Value |", "| --- | --- |",
              f"| Unsafe calls caught (blocked or escalated) | {_pct(current['caught'])} |",
              f"| Unsafe calls blocked outright | {_pct(current['blocked'])} |",
              f"| Safe calls blocked (false-block rate) | {_pct(current['false_block'])} |",
              f"| Safe calls sent to a human | {_pct(current['escalated_safe'])} |",
              f"| Latency p50 / p95 | {latency['p50_ms']:.1f} ms / {latency['p95_ms']:.1f} ms |", "",
              "## Block threshold sweep", "",
              "Blocking safe calls is how security controls get switched off. Pick the row your",
              "team can live with, then set it in `config/gate.json`.", "",
              "| Block at | Caught | Blocked | False blocks | Safe escalated |", "| --- | --- | --- | --- | --- |"]
    for block in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95):
        r = rates(block, min(t["escalate"], block))
        lines.append(f"| {block:.2f} | {_pct(r['caught'])} | {_pct(r['blocked'])} | "
                     f"{_pct(r['false_block'])} | {_pct(r['escalated_safe'])} |")
    act = lambda r: r["action"]
    misses = [r for r in rows if (r["label"] == "block" and act(r) == "allow")
              or (r["label"] == "allow" and act(r) == "block")]
    lines += ["", f"## Misses at the configured thresholds ({len(misses)})", "",
              "| Call | Tool | Label | Decision | P(unsafe) |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {r['id']} | `{r['tool']}` | {r['label']} | {r['action']} | {r['unsafe']:.2f} |" for r in misses]
    (out / "gate.md").write_text("\n".join(lines) + "\n")
    return current
