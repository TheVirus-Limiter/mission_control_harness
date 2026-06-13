"""Model registry — judges AND pipeline presets.

Every entry is a different provider/model. The same registry is used two ways:
  * the Rehearsal panel draws its multi-model jury from it (build_real_judges),
  * the pipeline worker (researcher/writer) can be ANY one of them via --preset.

NVIDIA NIM rides the OpenAI-compatible endpoint, so one NVIDIA_API_KEY lights up
the whole NVIDIA barrage. Every model id below was verified live against the NIM
catalog (stale ids were dropped). Add one: drop a line in PRESETS, no harness
change -- that is the swappability guarantee made literal.

Strictness is declared config (see STRICTNESS_RULES in workers/claude_worker.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1"
OLLAMA_DEFAULT = "http://localhost:11434/v1"


@dataclass(frozen=True)
class JudgeConfig:
    name: str          # display name (reviewer card + timeline)
    provider: str      # "anthropic" | "openai" | "nvidia" | "ollama"
    model: str
    env_key: str
    base_url: Optional[str] = None
    strictness: str = "normal"   # "strict" | "normal" | "lenient"
    key: str = ""      # short cli/ui key for --preset
    vendor: str = ""   # for UI grouping/icon: anthropic/openai/meta/mistral/...


# The declared barrage. Anthropic + OpenAI + a verified spread of NVIDIA NIM
# models (Meta Llama, Mistral, Microsoft Phi, OpenAI gpt-oss, Qwen).
PRESETS: list[JudgeConfig] = [
    JudgeConfig("Claude Haiku 4.5", "anthropic", "claude-haiku-4-5-20251001",
                "ANTHROPIC_API_KEY", None, "normal", "claude", "anthropic"),
    JudgeConfig("GPT-4o mini", "openai", "gpt-4o-mini",
                "OPENAI_API_KEY", None, "normal", "gpt", "openai"),
    JudgeConfig("Llama 3.3 70B", "nvidia", "meta/llama-3.3-70b-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "strict", "llama33", "meta"),
    JudgeConfig("Llama 3.1 70B", "nvidia", "meta/llama-3.1-70b-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "normal", "llama31", "meta"),
    JudgeConfig("Llama 3.1 8B", "nvidia", "meta/llama-3.1-8b-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "lenient", "llama31s", "meta"),
    JudgeConfig("Llama 3.2 3B", "nvidia", "meta/llama-3.2-3b-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "lenient", "llama32", "meta"),
    JudgeConfig("Llama 4 Maverick", "nvidia", "meta/llama-4-maverick-17b-128e-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "strict", "llama4", "meta"),
    JudgeConfig("Mixtral 8x7B", "nvidia", "mistralai/mixtral-8x7b-instruct-v0.1",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "normal", "mixtral", "mistral"),
    JudgeConfig("Phi-4 mini", "nvidia", "microsoft/phi-4-mini-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "normal", "phi4", "microsoft"),
    JudgeConfig("GPT-OSS 20B", "nvidia", "openai/gpt-oss-20b",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "normal", "gptoss", "openai"),
    JudgeConfig("Qwen3 Next 80B", "nvidia", "qwen/qwen3-next-80b-a3b-instruct",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "strict", "qwen3", "qwen"),
    JudgeConfig("Nemotron Super 49B", "nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "strict", "nemotron", "nvidia"),
    JudgeConfig("GLM 5.1", "nvidia", "z-ai/glm-5.1",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "normal", "glm", "zai"),
    JudgeConfig("Mistral Large 3", "nvidia", "mistralai/mistral-large-3-675b-instruct-2512",
                "NVIDIA_API_KEY", NIM_ENDPOINT, "strict", "mistral-lg", "mistral"),
]

# Back-compat alias: the panel IS the preset roster.
PANEL = PRESETS
BY_KEY = {p.key: p for p in PRESETS}

# (label, model, strictness) view of the NVIDIA barrage, for docs/tests.
NIM_CATALOG = [(p.name, p.model, p.strictness) for p in PRESETS if p.provider == "nvidia"]

# Optional LOCAL judges via Ollama, activated only when OLLAMA_HOST is set.
OLLAMA_CATALOG = [
    ("ollama-llama3.1", "llama3.1", "normal"),
    ("ollama-mistral", "mistral", "normal"),
]


def get_preset(key: str) -> Optional[JudgeConfig]:
    return BY_KEY.get(key)


def available_presets() -> list[JudgeConfig]:
    """Presets whose API key is actually present."""
    return [p for p in PRESETS if os.environ.get(p.env_key)]


def roster_names() -> list[str]:
    return [p.name for p in PRESETS]


def _ollama_judges() -> list[JudgeConfig]:
    return [JudgeConfig(n, "ollama", m, "OLLAMA_HOST", None, s, "ol-" + n, "ollama")
            for (n, m, s) in OLLAMA_CATALOG]


def build_real_judges(faulty_grader: bool = False) -> list:
    """One real judge per config whose key/host is present. A single
    NVIDIA_API_KEY activates the whole barrage. MAX_JUDGES caps panel size to
    bound cost (default 6). Returns [] if nothing available -> engine uses mocks."""
    from workers.claude_worker import RealJudge

    roster = list(PRESETS)
    if os.environ.get("OLLAMA_HOST"):
        roster += _ollama_judges()
    judges = [RealJudge(cfg) for cfg in roster if os.environ.get(cfg.env_key)]

    limit = 6
    cap = os.environ.get("MAX_JUDGES")
    if cap:
        try:
            limit = max(1, int(cap))
        except ValueError:
            limit = 6
    judges = judges[:limit]

    if faulty_grader and judges:
        from workers.mock import FaultyReviewer
        judges[0] = FaultyReviewer(judges[0].name + " [FAULTY]")
    return judges
