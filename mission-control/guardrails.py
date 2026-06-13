"""PILLAR 2 — Guardrails.

The declared rulebook. Every value a checkpoint enforces lives here, and every
value here comes from the mission YAML's ``guardrails:`` block. The harness
never invents a rule and a checkpoint never hard-codes one -- it reads it from
this dataclass.

That separation is the whole point: a reviewer can open the mission file and see
exactly what the system will and will not allow, without reading any code.

There is NO worker logic in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Guardrails:
    """The declared limits for a mission, loaded from ``guardrails:``."""

    banned_claims: list[str] = field(default_factory=list)
    margin_floor: float = 0.0
    citation_required: bool = True
    human_hold_required: bool = True
    recipient_allowlist: list[str] = field(default_factory=list)

    @classmethod
    def from_mission(cls, mission: dict) -> "Guardrails":
        g = (mission or {}).get("guardrails", {}) or {}
        return cls(
            banned_claims=list(g.get("banned_claims", [])),
            margin_floor=float(g.get("margin_floor", 0.0)),
            citation_required=bool(g.get("citation_required", True)),
            human_hold_required=bool(g.get("human_hold_required", True)),
            recipient_allowlist=list(g.get("recipient_allowlist", [])),
        )

    def as_dict(self) -> dict:
        return {
            "banned_claims": self.banned_claims,
            "margin_floor": self.margin_floor,
            "citation_required": self.citation_required,
            "human_hold_required": self.human_hold_required,
            "recipient_allowlist": self.recipient_allowlist,
        }
