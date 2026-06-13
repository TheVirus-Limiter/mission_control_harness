"""Judge configuration for the Rehearsal panel.

The panel is DECLARED here, not in the engine. Each judge is a different
provider/model at a declared strictness profile. Swapping a model, adding a
judge, or changing a strictness profile is a one-line edit in this file and
requires NO change to the harness -- that is the swappability guarantee for the
review side.

Strictness is declared config, not a vibe: each profile maps to an explicit
instruction block (see STRICTNESS_RULES in workers/claude_worker.py) that
changes how aggressively a criterion is failed. Every judge still returns a
verdict over the FULL declared rubric, so meta_check can audit it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class JudgeConfig:
    name: str          # display name shown on the reviewer card + timeline
    provider: str      # "anthropic" | "openai" | "nvidia"
    model: str
    env_key: str       # which API key must be present for this judge to run
    base_url: Optional[str] = None
    strictness: str = "normal"   # "strict" | "normal" | "lenient"


# The declared multi-model panel. Three different providers, by design.
PANEL: list[JudgeConfig] = [
    JudgeConfig("anthropic-claude", "anthropic", "claude-haiku-4-5-20251001",
                "ANTHROPIC_API_KEY", None, "normal"),
    JudgeConfig("openai-gpt", "openai", "gpt-4o-mini",
                "OPENAI_API_KEY", None, "normal"),
    JudgeConfig("nvidia-nim", "nvidia", "meta/llama-3.3-70b-instruct",
                "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "strict"),
]


def build_real_judges(faulty_grader: bool = False) -> list:
    """Instantiate one real judge per config whose API key is present. Judges
    whose key is missing are simply skipped (the harness degrades gracefully).
    Returns an empty list if no provider key is available -- the engine then
    falls back to the deterministic mock panel."""
    from workers.claude_worker import RealJudge

    judges = []
    for cfg in PANEL:
        if not os.environ.get(cfg.env_key):
            continue
        judges.append(RealJudge(cfg))

    if faulty_grader and judges:
        # For `--real --faulty-grader` we still want to demonstrate the meta_check
        # catching a broken grader, so we swap in the deterministic faulty judge.
        from workers.mock import FaultyReviewer

        judges[0] = FaultyReviewer(judges[0].name + " (FAULTY)")
    return judges
