"""The swappable Worker interface.

A worker is the *commodity* in this system -- the thing the harness governs. The
harness knows exactly one thing about it:

    run(task: dict, feedback: str | None) -> dict

``task`` is the material the harness hands in. ``feedback`` is the checkpoint
critique on a retry; a good worker uses it to fix its previous output. The
return is a plain dict payload the harness wraps in an Envelope.

Because this is the only contract, ANY agent or model can be dropped in -- a
deterministic mock, a Claude agent, an OpenAI model, an NVIDIA NIM endpoint --
with zero changes to the harness. This file deliberately contains no business
logic, no prompts and no model calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class Worker(ABC):
    #: Human-readable identity, surfaced in the certificate and the timeline.
    name: str = "worker"

    @abstractmethod
    def run(self, task: dict, feedback: Optional[str] = None) -> dict:
        """Produce an output payload for ``task``.

        On a retry the harness passes the checkpoint critique as ``feedback``;
        the worker is expected to use it to repair its output.
        """
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """Whether this worker can run (e.g. its API key is present).

        Mock workers are always available. Real workers override this so the
        harness can degrade gracefully instead of crashing on a missing key.
        """
        return True
