"""Judge configuration for the Rehearsal panel.

The panel is DECLARED here, not in the engine. Each judge is a different
provider/model at a declared strictness profile. Swapping a model, adding a
judge, or changing a strictness profile is a one-line edit in this file and
requires NO change to the harness -- that is the swappability guarantee for the
review side.

NVIDIA NIM is the clearest demonstration of this: a whole catalog of open models
(DeepSeek, Mistral/Mixtral, Qwen, Gemma, Phi, Llama, Nemotron, ...) is served
through the SAME OpenAI-compatible endpoint with a SINGLE `NVIDIA_API_KEY`, so
one key lights up the entire bunch. Browse the catalog at
https://build.nvidia.com/explore/discover and paste the model id below.

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
    provider: str      # "anthropic" | "openai" | "nvidia" | "ollama"
    model: str
    env_key: str       # which env var must be present for this judge to run
    base_url: Optional[str] = None
    strictness: str = "normal"   # "strict" | "normal" | "lenient"


# NVIDIA NIM speaks the OpenAI protocol on this endpoint. One key, many models.
NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1"

# The NVIDIA NIM catalog. Each tuple is (display name, NIM model id, strictness).
# All of these are gated by a single NVIDIA_API_KEY, so providing it activates
# every one of them in the panel. Add/remove a line -> the panel changes, the
# harness does not.
NIM_CATALOG: list[tuple[str, str, str]] = [
    ("nim-nemotron-70b",        "nvidia/llama-3.1-nemotron-70b-instruct",   "strict"),
    ("nim-deepseek-r1",         "deepseek-ai/deepseek-r1",                  "strict"),
    ("nim-deepseek-r1-distill", "deepseek-ai/deepseek-r1-distill-qwen-32b", "normal"),
    ("nim-llama-3.1-405b",      "meta/llama-3.1-405b-instruct",             "strict"),
    ("nim-llama-3.3-70b",       "meta/llama-3.3-70b-instruct",              "normal"),
    ("nim-mistral-large",       "mistralai/mistral-large-2-instruct",       "normal"),
    ("nim-mixtral-8x22b",       "mistralai/mixtral-8x22b-instruct-v0.1",    "normal"),
    ("nim-mistral-7b",          "mistralai/mistral-7b-instruct-v0.3",       "lenient"),
    ("nim-qwen2.5-coder-32b",   "qwen/qwen2.5-coder-32b-instruct",          "normal"),
    ("nim-gemma-2-27b",         "google/gemma-2-27b-it",                    "normal"),
    ("nim-phi-3.5-moe",         "microsoft/phi-3.5-moe-instruct",           "lenient"),
]

# Optional LOCAL judges via Ollama. Ollama is a local runtime (not a NIM model),
# also OpenAI-compatible. These activate only when OLLAMA_HOST is set, e.g.
#   OLLAMA_HOST=http://localhost:11434/v1
OLLAMA_CATALOG: list[tuple[str, str, str]] = [
    ("ollama-llama3.1", "llama3.1",    "normal"),
    ("ollama-mistral",  "mistral",     "normal"),
    ("ollama-deepseek", "deepseek-r1", "strict"),
]


def _nim_judges() -> list[JudgeConfig]:
    return [JudgeConfig(name, "nvidia", model, "NVIDIA_API_KEY", NIM_ENDPOINT, strict)
            for (name, model, strict) in NIM_CATALOG]


def _ollama_judges() -> list[JudgeConfig]:
    return [JudgeConfig(name, "ollama", model, "OLLAMA_HOST", None, strict)
            for (name, model, strict) in OLLAMA_CATALOG]


# One hosted judge per major provider, then the whole NIM bunch.
CORE: list[JudgeConfig] = [
    JudgeConfig("anthropic-claude", "anthropic", "claude-haiku-4-5-20251001",
                "ANTHROPIC_API_KEY", None, "normal"),
    JudgeConfig("openai-gpt", "openai", "gpt-4o-mini",
                "OPENAI_API_KEY", None, "normal"),
]

# The declared panel: Anthropic + OpenAI + every NVIDIA NIM model. (Ollama is
# appended at build time only when OLLAMA_HOST is set, so it stays opt-in.)
PANEL: list[JudgeConfig] = CORE + _nim_judges()


def roster_names() -> list[str]:
    """Display names of the full declared panel -- used to mirror the panel as a
    mock bench (``--full-panel``) so the bunch is visible without any API key."""
    return [c.name for c in PANEL]


def build_real_judges(faulty_grader: bool = False) -> list:
    """Instantiate one real judge per config whose key/host is present. Judges
    whose key is missing are skipped (graceful degradation). A single
    NVIDIA_API_KEY activates the entire NIM bunch. Set MAX_JUDGES to cap the
    panel size for cost control. Returns [] if nothing is available, so the
    engine falls back to the deterministic mock panel."""
    from workers.claude_worker import RealJudge

    roster = list(PANEL)
    if os.environ.get("OLLAMA_HOST"):
        roster += _ollama_judges()

    judges = [RealJudge(cfg) for cfg in roster if os.environ.get(cfg.env_key)]

    # Cap panel size to bound real-model cost (admission probes EVERY judge 5x).
    # Defaults to 6; set MAX_JUDGES to raise/lower it.
    limit = 6
    cap = os.environ.get("MAX_JUDGES")
    if cap:
        try:
            limit = max(1, int(cap))
        except ValueError:
            limit = 6
    judges = judges[:limit]

    if faulty_grader and judges:
        # For `--real --faulty-grader` swap in the deterministic faulty judge so
        # meta_check still has a broken grader to catch.
        from workers.mock import FaultyReviewer

        judges[0] = FaultyReviewer(judges[0].name + " (FAULTY)")
    return judges
