"""Small calls to a chat model for the companion's own background work (learning memories and
habits, tidying, drafting a skill, ideas): the cheapest one in 模型, and a JSON answer read safely."""

from __future__ import annotations

import json
import re
from typing import Any


def chat_target() -> dict[str, Any]:
    """``provider`` and ``model`` for background work: the cheapest chat model in 模型 that works
    (no one waits on it, so speed doesn't matter); empty: Hermes's default."""
    from .models import _pick, read_models, runtime_provider

    data = read_models()
    name = _pick(data, list(data["presets"]), cheapest=True)
    if not name:
        return {}
    preset = data["presets"][name]
    return {"provider": runtime_provider(name, preset), "model": preset["model"]}


def ask(system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0, timeout: int = 120) -> str:
    from muse.llm.auxiliary import call_llm

    response = call_llm(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens, temperature=temperature, timeout=timeout, reasoning_config={"enabled": False},
        **chat_target())
    return response.choices[0].message.content or ""


def ask_json(system: str, user: str, **kwargs: Any) -> dict[str, Any]:
    """The JSON object in the answer, or ``{}`` when there is none."""
    match = re.search(r"\{[\s\S]*\}", ask(system, user, **kwargs))
    try:
        value = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
