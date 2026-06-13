"""Gate 2 -- Rehearsal / The Digital Twin.

Before a post touches the world it is rehearsed. The gate:

  1. builds the EXACT, byte-identical outbound payload that Gate 3 would send
     (it calls the very same ``build_x_payload``), inside a sandbox with network
     egress disabled -- proving the payload can be constructed and inspected
     without any possibility of it actually being sent;
  2. runs a multi-model review panel (the judges are injected by the engine, so
     the panel is swappable);
  3. audits EVERY judge's verdict with the deterministic ``meta_check`` -- a
     malformed or hallucinated verdict means the judge is faulty, so it is
     re-run up to the reviewer-retry budget, and if it stays broken the quality
     gate is down and we escalate (fail closed);
  4. aggregates by UNANIMOUS CONSENT -- the post is publish-eligible only if
     every judge passes every criterion. If any judge flags any criterion the
     post is HELD. There is no averaging anywhere.

This file orchestrates inspection; it contains no content-generation logic.
"""

from __future__ import annotations

import contextlib
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from alarms import Alarm, AlarmType
from checkpoints import meta_check
from materials import Envelope
from models.judges import tier_rank
from gates.action import build_x_payload, render_post


class RehearsalEscalation(Exception):
    """Raised when the quality gate cannot reach a verdict (a judge stays
    broken). The engine turns this into a fail-closed halt."""

    def __init__(self, alarm: Alarm):
        super().__init__(alarm.context)
        self.alarm = alarm


@contextlib.contextmanager
def network_disabled():
    """Disable outbound sockets for the duration of the block. Used to prove the
    digital-twin payload is built without touching the network."""
    real_socket = socket.socket
    real_create = socket.create_connection

    def _blocked(*args, **kwargs):
        raise RuntimeError("network egress is disabled inside the Rehearsal sandbox")

    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = real_socket  # type: ignore[assignment]
        socket.create_connection = real_create  # type: ignore[assignment]


_RANK_NAME = {0: "lexical", 1: "standard", 2: "deep"}


class Rehearsal:
    def __init__(self, store, guardrails, rubric: list[str], reviewer_retries: int = 2,
                 handle: str = "@yourbrand", criterion_tiers: Optional[dict] = None):
        self.store = store
        self.guardrails = guardrails
        self.rubric = list(rubric)
        self.reviewer_retries = int(reviewer_retries)
        self.handle = handle
        # criterion -> minimum tier RANK allowed to vote (default 0 = lexical,
        # i.e. everyone votes -> backward-compatible unanimous consent).
        self.criterion_tiers = dict(criterion_tiers or {})

    def _assigned(self, judge) -> list[str]:
        """The criteria THIS judge is capable enough (high enough tier) to vote on."""
        jt = tier_rank(getattr(judge, "profile", "deep"))
        return [c for c in self.rubric if jt >= self.criterion_tiers.get(c, 0)]

    # -- per-judge review (only its assigned criteria) + meta_check + retry ---
    def _review(self, judge, text: str, run_id: str, assigned: list[str]) -> dict:
        if not assigned:  # this judge's tier covers no criterion -> it casts no vote
            v = {"overall": "pass", "criteria": {}, "reasons": {}, "citations": {},
                 "comment": "(no criteria at my tier)"}
            self.store.log(run_id, "rehearsal", "verdict", judge.name, ok=True, detail=v)
            return v
        attempt = 0
        while True:
            attempt += 1
            try:
                verdict = judge.run({"rubric": assigned, "text": text,
                                     "post": render_post(build_x_payload(text))})
            except Exception as e:
                verdict = {"_error": str(e)}

            # meta_check audits the judge against ONLY its assigned criteria, so it
            # is never faulted for omitting a criterion it was not asked about.
            audit = meta_check(Envelope(run_id, "rehearsal", verdict),
                               {"rubric": assigned, "text": text})
            self.store.log(run_id, "rehearsal", "check", f"meta_check:{judge.name}",
                           ok=audit.ok, detail={"attempt": attempt, "assigned": assigned, **audit.evidence})

            if audit.ok:
                self.store.log(run_id, "rehearsal", "verdict", judge.name,
                               ok=(verdict.get("overall") == "pass"), detail=verdict)
                return verdict

            fault = Alarm(AlarmType.REVIEWER_FAULT,
                          f"judge '{judge.name}' produced a malformed/hallucinated verdict: "
                          f"{audit.evidence.get('problems')}", "rehearsal")
            self.store.log(run_id, "rehearsal", "alarm", fault.type.value, ok=False,
                           detail=fault.as_dict())
            if attempt > self.reviewer_retries:
                esc = Alarm(AlarmType.ESCALATE_HUMAN,
                            f"judge '{judge.name}' still faulty after {attempt} attempts; "
                            "the quality gate is down -- refusing to proceed", "rehearsal")
                self.store.log(run_id, "rehearsal", "alarm", esc.type.value, ok=False, detail=esc.as_dict())
                raise RehearsalEscalation(esc)
            self.store.log(run_id, "rehearsal", "revision", f"rerun-judge:{judge.name}",
                           ok=False, detail={"attempt": attempt})

    # -- the gate -----------------------------------------------------------
    def run(self, run_id: str, text: str, judges: list) -> dict:
        # 1) digital twin: byte-identical payload, built with egress disabled.
        with network_disabled():
            payload = build_x_payload(text)
            rendered = render_post(payload, author=self.handle)
        self.store.log(run_id, "rehearsal", "gate", "digital-twin", ok=True,
                       detail={"payload": payload, "rendered": rendered,
                               "egress": "disabled", "byte_len": len(text)})

        assigned = {j.name: self._assigned(j) for j in judges}
        profiles = {j.name: getattr(j, "profile", "deep") for j in judges}
        # which judges may vote on each criterion (its eligible voter set)
        voters = {c: [j.name for j in judges if c in assigned[j.name]] for c in self.rubric}

        # FAIL CLOSED: a criterion with zero eligible voters is a config error --
        # never let a (possibly safety-critical) criterion go unjudged.
        unjudged = [c for c, vs in voters.items() if not vs]
        if unjudged:
            esc = Alarm(AlarmType.CONFIG_ERROR,
                        f"criteria {unjudged} have no judge at their required tier; "
                        "refusing to pass them unjudged", "rehearsal")
            self.store.log(run_id, "rehearsal", "alarm", esc.type.value, ok=False, detail=esc.as_dict())
            raise RehearsalEscalation(esc)

        # 2 + 3) review every judge CONCURRENTLY (independent, I/O-bound).
        verdicts: dict[str, dict] = {}
        escalation: Optional[RehearsalEscalation] = None
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(judges)))) as ex:
            futs = {ex.submit(self._review, j, text, run_id, assigned[j.name]): j for j in judges}
            for fut, judge in futs.items():
                try:
                    verdicts[judge.name] = fut.result()
                except RehearsalEscalation as e:
                    escalation = escalation or e
        if escalation:
            raise escalation
        verdicts = {j.name: verdicts[j.name] for j in judges if j.name in verdicts}

        # 4) consent: each criterion is unanimous WITHIN its eligible voters; the
        # post is eligible iff every criterion passes. No averaging anywhere.
        held, outcomes = [], {}
        for c in self.rubric:
            flaggers = []
            for jn in voters[c]:
                if verdicts.get(jn, {}).get("criteria", {}).get(c) == "fail":
                    flaggers.append(jn)
                    held.append({"judge": jn, "criterion": c, "tier": profiles.get(jn, "deep"),
                                 "reason": verdicts[jn].get("reasons", {}).get(c, "")})
            outcomes[c] = {"min_tier": _RANK_NAME.get(self.criterion_tiers.get(c, 0), "lexical"),
                           "voters": voters[c], "passed": not flaggers, "flagged_by": flaggers}
        eligible = not held

        result = {"eligible": eligible, "held": held, "verdicts": verdicts,
                  "payload": payload, "rendered": rendered, "rubric": self.rubric,
                  "judge_profiles": profiles, "assigned": assigned, "criteria_outcomes": outcomes}
        self.store.log(run_id, "rehearsal", "panel", "tiered-consent", ok=eligible,
                       detail={"eligible": eligible, "held": held, "judges": list(verdicts.keys()),
                               "criteria_outcomes": outcomes})
        self.store.save_output(Envelope(run_id, "rehearsal", result))
        return result
