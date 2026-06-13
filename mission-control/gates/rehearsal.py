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
from typing import Optional

from alarms import Alarm, AlarmType
from checkpoints import meta_check
from materials import Envelope
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


class Rehearsal:
    def __init__(self, store, guardrails, rubric: list[str], reviewer_retries: int = 2):
        self.store = store
        self.guardrails = guardrails
        self.rubric = list(rubric)
        self.reviewer_retries = int(reviewer_retries)

    # -- per-judge review with meta_check audit + retry ----------------------
    def _review(self, judge, text: str, run_id: str) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                verdict = judge.run({"rubric": self.rubric, "text": text,
                                     "post": render_post(build_x_payload(text))})
            except Exception as e:
                verdict = {"_error": str(e)}

            audit = meta_check(Envelope(run_id, "rehearsal", verdict),
                               {"rubric": self.rubric, "text": text})
            self.store.log(run_id, "rehearsal", "check", f"meta_check:{judge.name}",
                           ok=audit.ok, detail={"attempt": attempt, **audit.evidence})

            if audit.ok:
                self.store.log(run_id, "rehearsal", "verdict", judge.name,
                               ok=(verdict.get("overall") == "pass"), detail=verdict)
                return verdict

            # --- the judge is faulty (branch 2 of the three-way routing) ---
            fault = Alarm(AlarmType.REVIEWER_FAULT,
                          f"judge '{judge.name}' produced a malformed/hallucinated verdict: "
                          f"{audit.evidence.get('problems')}", "rehearsal")
            self.store.log(run_id, "rehearsal", "alarm", fault.type.value, ok=False,
                           detail=fault.as_dict())

            if attempt > self.reviewer_retries:
                esc = Alarm(AlarmType.ESCALATE_HUMAN,
                            f"judge '{judge.name}' still faulty after {attempt} attempts; "
                            "the quality gate is down -- refusing to proceed", "rehearsal")
                self.store.log(run_id, "rehearsal", "alarm", esc.type.value, ok=False,
                               detail=esc.as_dict())
                raise RehearsalEscalation(esc)

            self.store.log(run_id, "rehearsal", "revision", f"rerun-judge:{judge.name}",
                           ok=False, detail={"attempt": attempt})

    # -- the gate -----------------------------------------------------------
    def run(self, run_id: str, text: str, judges: list) -> dict:
        # 1) digital twin: byte-identical payload, built with egress disabled.
        with network_disabled():
            payload = build_x_payload(text)
            rendered = render_post(payload)
        self.store.log(run_id, "rehearsal", "gate", "digital-twin", ok=True,
                       detail={"payload": payload, "rendered": rendered,
                               "egress": "disabled", "byte_len": len(text)})

        # 2 + 3) review every judge, auditing each verdict.
        verdicts: dict[str, dict] = {}
        for judge in judges:
            verdicts[judge.name] = self._review(judge, text, run_id)  # may escalate

        # 4) unanimous consent -- HELD if ANY judge flags ANY criterion.
        held = []
        for jname, v in verdicts.items():
            for crit, val in v.get("criteria", {}).items():
                if val == "fail":
                    held.append({"judge": jname, "criterion": crit,
                                 "reason": v.get("reasons", {}).get(crit, "")})
        eligible = not held

        result = {"eligible": eligible, "held": held, "verdicts": verdicts,
                  "payload": payload, "rendered": rendered, "rubric": self.rubric}
        self.store.log(run_id, "rehearsal", "panel", "unanimous-consent", ok=eligible,
                       detail={"eligible": eligible, "held": held,
                               "judges": list(verdicts.keys())})
        # Persist the panel result so the dashboard and replay can read it.
        self.store.save_output(Envelope(run_id, "rehearsal", result))
        return result
