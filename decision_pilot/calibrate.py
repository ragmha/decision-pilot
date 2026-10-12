"""Calibration and threshold selection from labelled replays."""

from __future__ import annotations

import hashlib
from statistics import median


def split(item_id: str) -> str:
    """Stable 50/50 split so thresholds are tuned and evaluated on different items."""
    return "tune" if hashlib.sha256(item_id.encode()).digest()[0] % 2 == 0 else "holdout"


def reliability(pairs: list[tuple[float, bool]], bins: int = 5) -> list[dict]:
    rows = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        bucket = [(p, ok) for p, ok in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if bucket:
            rows.append({
                "range": f"{lo:.1f}-{hi:.1f}",
                "n": len(bucket),
                "confidence": sum(p for p, _ in bucket) / len(bucket),
                "accuracy": sum(ok for _, ok in bucket) / len(bucket),
            })
    return rows


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 5) -> float:
    if not pairs:
        return 0.0
    return sum(r["n"] * abs(r["confidence"] - r["accuracy"]) for r in reliability(pairs, bins)) / len(pairs)


def at_threshold(pairs: list[tuple[float, bool]], threshold: float) -> dict:
    kept = [ok for p, ok in pairs if p >= threshold]
    return {
        "threshold": threshold,
        "coverage": len(kept) / len(pairs) if pairs else 0.0,
        "precision": sum(kept) / len(kept) if kept else None,
        "n": len(kept),
    }


def pick_threshold(pairs: list[tuple[float, bool]], target: float, min_n: int = 3) -> float | None:
    """Lowest confidence cutoff whose kept decisions meet the target precision.

    Returns None when no cutoff meets the target: the honest answer is then
    "don't automate this decision yet".
    """
    for threshold in sorted({round(p, 3) for p, _ in pairs}):
        row = at_threshold(pairs, threshold)
        if row["n"] >= min_n and row["precision"] >= target:
            return threshold
    return None


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def latency_summary(values: list[float]) -> dict:
    return {"p50_ms": median(values) if values else 0.0, "p95_ms": percentile(values, 0.95)}
