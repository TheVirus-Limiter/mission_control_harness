"""PILLAR 4 — Alarms.

When something goes wrong the harness does not print a string and hope. It
raises a *structured* alarm: a named type (enum), a severity, a context string,
the stage it fired in, and a recommended action derived from the type. Alarms
serialise to a dict for logging into the store, so the timeline and dashboard
render the exact same structured object.

There is NO worker logic in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlarmType(str, Enum):
    UNGROUNDED_CLAIM = "UNGROUNDED_CLAIM"
    BANNED_CLAIM = "BANNED_CLAIM"
    MARGIN_BREACH = "MARGIN_BREACH"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    REVIEWER_FAULT = "REVIEWER_FAULT"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    FORBIDDEN_ACTION = "FORBIDDEN_ACTION"
    CERTIFICATION_FAILED = "CERTIFICATION_FAILED"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    SCHEMA_INVALID = "SCHEMA_INVALID"


# Default severity per type. A checkpoint can override when it raises.
_DEFAULT_SEVERITY: dict[AlarmType, Severity] = {
    AlarmType.UNGROUNDED_CLAIM: Severity.HIGH,
    AlarmType.BANNED_CLAIM: Severity.HIGH,
    AlarmType.MARGIN_BREACH: Severity.HIGH,
    AlarmType.CONTENT_REJECTED: Severity.MEDIUM,
    AlarmType.REVIEWER_FAULT: Severity.HIGH,
    AlarmType.INJECTION_DETECTED: Severity.CRITICAL,
    AlarmType.FORBIDDEN_ACTION: Severity.CRITICAL,
    AlarmType.CERTIFICATION_FAILED: Severity.CRITICAL,
    AlarmType.AWAITING_HUMAN: Severity.MEDIUM,
    AlarmType.BUDGET_EXCEEDED: Severity.HIGH,
    AlarmType.ESCALATE_HUMAN: Severity.CRITICAL,
    AlarmType.SCHEMA_INVALID: Severity.HIGH,
}

# The recommended action is a property of the *type*, not invented per-call.
_RECOMMENDED_ACTION: dict[AlarmType, str] = {
    AlarmType.UNGROUNDED_CLAIM: "Return critique to the writer; require an inline [fN] citation for every factual sentence.",
    AlarmType.BANNED_CLAIM: "Return critique to the writer; strip the banned phrase and re-submit.",
    AlarmType.MARGIN_BREACH: "Reprice above the declared margin floor or escalate to a human for an exception.",
    AlarmType.CONTENT_REJECTED: "Send the panel's held criteria back to the writer to revise, then re-run Rehearsal.",
    AlarmType.REVIEWER_FAULT: "Re-run the faulty judge; if it stays broken the quality gate is down -- escalate, do not proceed.",
    AlarmType.INJECTION_DETECTED: "Refuse to certify the agent; do not assign it to any stage.",
    AlarmType.FORBIDDEN_ACTION: "Refuse to certify the agent; the agent attempted an irreversible action without approval.",
    AlarmType.CERTIFICATION_FAILED: "Block assignment of this agent; review the failed attacks before re-attempting.",
    AlarmType.AWAITING_HUMAN: "Pause and request explicit human approval; record the decision before any post.",
    AlarmType.BUDGET_EXCEEDED: "Stop revising; fail closed and escalate to a human.",
    AlarmType.ESCALATE_HUMAN: "Stop. A gate could not reach a verdict -- hand the run to a human.",
    AlarmType.SCHEMA_INVALID: "Reject the stage output; the required fields are missing or empty.",
}


@dataclass
class Alarm:
    type: AlarmType
    context: str
    stage: str
    severity: Optional[Severity] = None

    def __post_init__(self):
        if self.severity is None:
            self.severity = _DEFAULT_SEVERITY[self.type]

    @property
    def recommended_action(self) -> str:
        return _RECOMMENDED_ACTION[self.type]

    def as_dict(self) -> dict:
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "stage": self.stage,
            "context": self.context,
            "recommended_action": self.recommended_action,
        }
