"""Decision providers: an offline mock and Microsoft Foundry's decision API.

Every provider takes the same request (a state plus typed questions) and
returns normalised answers:

    choice -> {"type": "choice", "choice": str, "probabilities": {option: p}}
    noul   -> {"type": "noul", "probability": p}
    score  -> {"type": "score", "score": float}
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any

FOUNDRY_PATH = "/providers/microsoft/v1/systemone"


class DecisionError(RuntimeError):
    """The provider could not return a usable decision."""


def provider_from_env() -> "Provider":
    name = os.environ.get("DECISION_PROVIDER") or (
        "foundry" if os.environ.get("AZURE_ENDPOINT") else "mock"
    )
    if name == "mock":
        return MockProvider()
    if name == "foundry":
        return FoundryProvider.from_env()
    raise DecisionError(f"Unknown DECISION_PROVIDER {name!r}. Use mock or foundry.")


class Provider:
    name = "provider"
    model = "unknown"

    def ask(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]:
        raise NotImplementedError


class FoundryProvider(Provider):
    name = "foundry"

    def __init__(self, endpoint: str, deployment: str, api_key: str | None, token: str | None,
                 timeout: float = 30):
        self.url = endpoint.rstrip("/") + FOUNDRY_PATH
        self.deployment = deployment
        self.model = deployment
        self.api_key = api_key
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "FoundryProvider":
        endpoint = os.environ.get("AZURE_ENDPOINT")
        deployment = os.environ.get("DEPLOYMENT_NAME")
        api_key = os.environ.get("AZURE_API_KEY")
        token = os.environ.get("AZURE_ENTRA_TOKEN")
        missing = [n for n, v in (("AZURE_ENDPOINT", endpoint), ("DEPLOYMENT_NAME", deployment)) if not v]
        if missing:
            raise DecisionError(f"Set {', '.join(missing)} to use the Foundry provider.")
        if not api_key and not token:
            raise DecisionError("Set AZURE_API_KEY or AZURE_ENTRA_TOKEN to use the Foundry provider.")
        return cls(endpoint, deployment, api_key, token)

    def ask(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]:
        body = json.dumps({"model": self.deployment, "state": state, "questions": questions}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["api-key"] = self.api_key
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise DecisionError(f"Decision API returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise DecisionError(f"Could not reach the decision API: {exc.reason}.") from exc
        except json.JSONDecodeError as exc:
            raise DecisionError("Decision API returned invalid JSON.") from exc
        if isinstance(payload, dict) and payload.get("model"):
            self.model = payload["model"]
        return normalise(payload, questions)


def _first(answer: dict, keys: tuple[str, ...]) -> Any:
    return next((answer[k] for k in keys if k in answer), None)


def normalise(payload: Any, questions: dict[str, dict]) -> dict[str, dict]:
    """Map a decision API response onto the shapes in this module's docstring.

    Microsoft's docs specify `answers.<name>.type` and `.choice`. The field that
    carries per-option probabilities isn't shown in the docs, so a few common
    names are accepted and unknown shapes fail loudly with the keys seen.
    """
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise DecisionError("Decision API response has no answers object.")
    result = {}
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise DecisionError(f"Decision API returned no answer for {name!r}.")
        kind = question["type"]
        if kind == "choice":
            probabilities = _first(answer, ("probabilities", "probs", "distribution", "scores"))
            choice = answer.get("choice")
            if not isinstance(probabilities, dict) or choice not in question["criteria"]:
                raise DecisionError(f"Unrecognised choice answer for {name!r}; keys: {sorted(answer)}.")
            result[name] = {"type": kind, "choice": choice,
                            "probabilities": {k: float(v) for k, v in probabilities.items()}}
        elif kind == "noul":
            probability = _first(answer, ("probability", "p", "value", "score"))
            if not isinstance(probability, (int, float)):
                raise DecisionError(f"Unrecognised noul answer for {name!r}; keys: {sorted(answer)}.")
            result[name] = {"type": kind, "probability": float(probability)}
        elif kind == "score":
            score = _first(answer, ("score", "value"))
            if not isinstance(score, (int, float)):
                raise DecisionError(f"Unrecognised score answer for {name!r}; keys: {sorted(answer)}.")
            result[name] = {"type": kind, "score": float(score)}
        else:
            raise DecisionError(f"Unsupported question type {kind!r}.")
    return result


# --- Offline mock -----------------------------------------------------------
#
# The mock is a keyword heuristic, not a model. It exists so the pipeline,
# reports and workflows run without credentials. Its numbers say nothing about
# Microsoft-Decision-1 or any other model.

WORD = re.compile(r"[a-z][a-z0-9_.-]+")
STOP = set("a an and are as at be by for from in is it of on or that the this to with when".split())

RISK = [
    (r"ignore (all |any )?(previous|prior) instructions|disregard (the )?(system|above)", 3.0),
    (r"rm -rf|mkfs|dd if=|:\(\)\{|chmod 777", 2.5),
    (r"curl [^|]*\| *(ba)?sh|wget [^|]*\| *(ba)?sh", 2.5),
    (r"\.ssh|id_rsa|\.aws/credentials|\.env\b|secrets?\b|api[_-]?key|token", 1.6),
    (r"pastebin|ngrok|requestbin|webhook\.site|\.onion", 2.0),
    (r"drop table|truncate table|delete from \w+;?\s*$", 2.2),
    (r"push --force|force-push|--no-verify|disable (branch )?protection", 1.8),
    (r"\bprod(uction)?\b", 0.8),
    (r"base64", 0.7),
]


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def _softmax(scores: dict[str, float], temperature: float) -> dict[str, float]:
    top = max(scores.values())
    exp = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


class MockProvider(Provider):
    name = "mock"
    model = "mock-keyword-heuristic"

    def ask(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]:
        text = state if isinstance(state, str) else json.dumps(state)
        words = _words(text)
        result = {}
        for name, question in questions.items():
            kind = question["type"]
            if kind == "choice":
                scores = {
                    option: len(words & _words(f"{option} {description}"))
                    for option, description in question["criteria"].items()
                }
                probabilities = _softmax(scores, temperature=0.9)
                choice = max(probabilities, key=probabilities.get)
                result[name] = {"type": kind, "choice": choice, "probabilities": probabilities}
            elif kind == "noul":
                risk = sum(weight for pattern, weight in RISK if re.search(pattern, text.lower()))
                result[name] = {"type": kind, "probability": 1 / (1 + math.exp(-(risk - 1.5)))}
            elif kind == "score":
                levels = question["criteria"]
                scores = {str(i): len(words & _words(level)) for i, level in enumerate(levels)}
                probabilities = _softmax(scores, temperature=0.9)
                result[name] = {"type": kind,
                                "score": sum(int(i) * p for i, p in probabilities.items())}
            else:
                raise DecisionError(f"Unsupported question type {kind!r}.")
        return result
