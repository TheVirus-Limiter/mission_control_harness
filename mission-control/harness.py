"""Mission Control -- the engine.

This is the harness: orchestration, the three-way failure routing, replay, the
NASA-style flight-log timeline, and the CLI. It governs workers; it does not do
their work. It owns the materials, reads the declared guardrails, runs the
deterministic checkpoints, raises structured alarms, and -- in later milestones
-- drives the three trust gates.

Run modes (CLI)
---------------
    python harness.py                       # launch mission, mock workers, no keys
    python harness.py --faulty-grader       # Rehearsal: meta_check catches a broken judge
    python harness.py --reject-demo         # Admission: a sketchy agent is refused
    python harness.py --real                # real models/agents (needs keys)
    python harness.py --mission missions/nonprofit.yaml
    python harness.py --replay-from rehearsal --run <id>
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from typing import Optional

# Resolve default paths relative to this file so `python harness.py` works from
# any working directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from alarms import Alarm, AlarmType, Severity
from checkpoints import CONTENT_CHECK_ALARM, REGISTRY, STAGE_SCHEMAS
from guardrails import Guardrails
from materials import Envelope, Store
from gates.admission import ProvingGround
from gates.action import ActionGate, HumanDeclined, render_post
from gates.rehearsal import Rehearsal, RehearsalEscalation
from workers.base import Worker
from workers.mock import (FaultyReviewer, HeldReviewer, MOCK_WORKERS, MedicalClaimWriter,
                          MockReviewer, SketchyAgent)

console = Console()


class HarnessHalt(Exception):
    """Raised when a gate cannot reach a verdict. The harness fails closed:
    it stops and escalates rather than guessing its way forward."""

    def __init__(self, alarm: Alarm):
        super().__init__(alarm.context)
        self.alarm = alarm


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class Harness:
    def __init__(
        self,
        mission_path: str,
        store: Store,
        *,
        real: bool = False,
        faulty_grader: bool = False,
        reject_demo: bool = False,
        block_demo: bool = False,
        full_panel: bool = False,
        approver=None,
    ):
        self.mission_path = mission_path
        with open(mission_path, "r", encoding="utf-8") as f:
            self.mission = yaml.safe_load(f)
        self.guardrails = Guardrails.from_mission(self.mission)
        self.store = store
        self.real = real
        self.faulty_grader = faulty_grader
        self.reject_demo = reject_demo
        self.block_demo = block_demo
        self.full_panel = full_panel
        self.budgets = self.mission.get("budgets", {})
        self.input = self.mission.get("input", {})
        self.stage_by_name = {s["name"]: s for s in self.mission.get("stages", [])}
        self.rubric = self.mission.get("rubric_criteria", [])
        admission_cfg = self.mission.get("admission", {}) or {}
        self.proving_ground = ProvingGround(
            store, self.guardrails, policy=admission_cfg.get("policy", "all_survived"))
        self._certified: dict[str, bool] = {}  # worker.name -> certified (per run)
        self.approver = approver or _cli_approver

    # -- worker assignment --------------------------------------------------
    def build_worker(self, role: str) -> Worker:
        """Build a worker for a role. The engine knows nothing about the worker
        beyond the Worker interface, which is what makes them swappable."""
        if self.real:
            try:
                from workers.claude_worker import build_real_worker

                w = build_real_worker(role)
                if w is not None and w.available:
                    return w
                console.print(f"[yellow]![/] real worker for '{role}' unavailable -- using mock")
            except Exception as e:  # pragma: no cover - defensive
                console.print(f"[yellow]![/] real workers unavailable ({e}) -- using mock")
        # --reject-demo drops a sketchy, uncertified agent into the writer slot
        # to prove Admission refuses it.
        if self.reject_demo and role == "writer":
            return SketchyAgent()

        # --block-demo drops a writer that keeps drafting an unsupported medical
        # claim, so Rehearsal holds it and it never reaches X.
        if self.block_demo and role == "writer":
            return MedicalClaimWriter()

        cls = MOCK_WORKERS.get(role)
        if cls is None:
            raise HarnessHalt(
                Alarm(AlarmType.ESCALATE_HUMAN, f"no worker registered for role '{role}'", role)
            )
        return cls()

    # -- Gate 1: certify an agent before trusting it with any work ----------
    def ensure_certified(self, worker: Worker, run_id: str) -> None:
        """Run the Admission gauntlet (once per worker per run). Refuse to use an
        agent that did not survive every attack -- fail closed."""
        if self._certified.get(worker.name):
            return
        cert = self.proving_ground.certify(worker, run_id)
        self._certified[worker.name] = cert.certified
        if not cert.certified:
            failed = [a.attack for a in cert.failed]
            alarm = Alarm(
                AlarmType.CERTIFICATION_FAILED,
                f"agent '{worker.name}' failed Admission on {failed}; refusing to assign it to a stage",
                "admission",
            )
            self.store.log(run_id, "admission", "alarm", alarm.type.value, ok=False,
                           detail={**alarm.as_dict(), "failed_attacks": [
                               {"attack": a.attack, "leaked": a.evidence} for a in cert.failed]})
            raise HarnessHalt(alarm)

    def assign_worker(self, stage: dict, run_id: str) -> Worker:
        """Assign a worker to a stage -- but only after Admission certifies it."""
        worker = self.build_worker(stage["worker"])
        self.ensure_certified(worker, run_id)
        return worker

    # -- the Rehearsal review panel (swappable; built from config) ----------
    def build_judges(self) -> list[Worker]:
        if self.real:
            try:
                from models.judges import build_real_judges

                judges = build_real_judges(self.faulty_grader)
                if judges:
                    return judges
                console.print("[yellow]![/] no real judges available -- using the mock panel")
            except Exception as e:  # pragma: no cover - defensive
                console.print(f"[yellow]![/] real judges unavailable ({e}) -- using the mock panel")

        if self.full_panel:
            # Mirror the full declared roster (Anthropic + OpenAI + the whole
            # NVIDIA NIM bunch) as deterministic mock judges, so the panel is
            # visible without any API key. Each is a real unanimous-consent
            # reviewer; the "(mock)" label keeps it honest.
            from models.judges import roster_names

            judges: list[Worker] = [MockReviewer(n + " (mock)") for n in roster_names()]
        else:
            judges = [
                MockReviewer("anthropic-claude (mock)"),
                MockReviewer("openai-gpt (mock)"),
                MockReviewer("nvidia-nim (mock)"),
            ]
        if self.faulty_grader:
            # one judge hallucinates a citation -> meta_check catches it.
            judges[1] = FaultyReviewer(judges[1].name + " [FAULTY]")
        if self.block_demo:
            # one judge legitimately holds an unsupported-claim criterion.
            judges[1] = HeldReviewer(judges[1].name + " [strict]")
        return judges

    # -- task + ctx assembly ------------------------------------------------
    def _build_task(self, stage: dict, accumulated: dict) -> dict:
        task = {
            "topic": self.input.get("topic"),
            "brand": self.input.get("brand"),
            "audience": self.input.get("audience"),
            "facts": accumulated.get("facts", {}),
            "rubric": self.mission.get("rubric_criteria", []),
            # declared policy handed to the writer as part of its brief (the
            # harness still enforces it independently at the checkpoint).
            "banned_claims": self.guardrails.banned_claims,
        }
        return task

    def _ctx_for(self, stage: dict, accumulated: dict) -> dict:
        return {
            "guardrails": self.guardrails,
            "facts": accumulated.get("facts", {}),
            "schema": STAGE_SCHEMAS.get(stage["name"]),
        }

    def _absorb(self, payload: dict, accumulated: dict) -> None:
        if "facts" in payload:
            accumulated["facts"] = payload["facts"]
        if "text" in payload:
            accumulated["draft"] = payload["text"]
        if "stated_total" in payload:
            accumulated["price"] = payload

    def _feedback_from(self, failed) -> str:
        lines = ["Your previous output failed these checks. Fix every one and resubmit:"]
        for r in failed:
            lines.append(f"- [{r.name}] evidence: {r.evidence}")
        return "\n".join(lines)

    # -- content stage (worker + checkpoints + revision routing) ------------
    def run_content_stage(self, stage: dict, run_id: str, accumulated: dict,
                          initial_feedback: Optional[str] = None) -> Envelope:
        name = stage["name"]
        worker = self.assign_worker(stage, run_id)
        checkpoints = stage.get("checkpoints", [])
        budget = int(self.budgets.get("writer_revisions", 1))

        self.store.log(run_id, name, "stage", "start", detail={"worker": worker.name})
        task = self._build_task(stage, accumulated)
        feedback: Optional[str] = initial_feedback
        attempt = 0

        while True:
            attempt += 1
            t0 = time.time()
            payload = worker.run(task, feedback)
            secs = round(time.time() - t0, 4)
            env = Envelope(
                run_id,
                name,
                payload,
                {"attempt": attempt, "seconds": secs,
                 "tokens": int(payload.get("_tokens", 0) or 0), "worker": worker.name},
            )

            ctx = self._ctx_for(stage, accumulated)
            results = []
            for cp_name in checkpoints:
                fn = REGISTRY[cp_name]
                res = fn(env, ctx)
                results.append(res)
                self.store.log(run_id, name, "check", cp_name, ok=res.ok,
                               detail={"attempt": attempt, **res.evidence})

            failed = [r for r in results if not r.ok]
            if not failed:
                self.store.save_output(env)
                self.store.log(run_id, name, "stage", "pass", ok=True,
                               detail={"attempt": attempt, "seconds": secs, "tokens": env.meta["tokens"]})
                self._absorb(payload, accumulated)
                return env

            # --- content failed: raise a structured alarm per failed check ---
            for r in failed:
                atype = AlarmType[CONTENT_CHECK_ALARM.get(r.name, "CONTENT_REJECTED")]
                alarm = Alarm(atype, context=f"{r.name} failed on attempt {attempt}: {r.evidence}", stage=name)
                self.store.log(run_id, name, "alarm", alarm.type.value, ok=False, detail=alarm.as_dict())

            # Three-way routing, branch 1 (bad content): rerun the writer with
            # the critique as feedback, up to the revision budget.
            if attempt > budget:  # budget revisions already spent -> fail closed
                self.store.log(run_id, name, "alarm", AlarmType.BUDGET_EXCEEDED.value, ok=False,
                               detail=Alarm(AlarmType.BUDGET_EXCEEDED,
                                            f"{name} still failing after {attempt} attempts", name).as_dict())
                esc = Alarm(AlarmType.ESCALATE_HUMAN, f"{name} could not pass its checkpoints within budget", name)
                self.store.log(run_id, name, "alarm", esc.type.value, ok=False, detail=esc.as_dict())
                raise HarnessHalt(esc)

            feedback = self._feedback_from(failed)
            self.store.log(run_id, name, "revision", f"revise->attempt-{attempt + 1}", ok=False,
                           detail={"feedback": feedback})

    # -- Gate 2: Rehearsal, with HELD -> writer routing (branch 3) -----------
    def run_rehearsal_stage(self, stage: dict, run_id: str, accumulated: dict) -> dict:
        name = stage["name"]
        self.store.log(run_id, name, "stage", "start", detail={"gate": "digital-twin + panel"})
        judges = self.build_judges()
        for judge in judges:  # judges are agents too -- certify them before trusting them
            self.ensure_certified(judge, run_id)

        rehearsal = Rehearsal(self.store, self.guardrails, self.rubric,
                              reviewer_retries=int(self.budgets.get("reviewer_retries", 2)))
        budget = int(self.budgets.get("writer_revisions", 1))
        held_revisions = 0

        while True:
            text = accumulated.get("draft", "")
            try:
                result = rehearsal.run(run_id, text, judges)
            except RehearsalEscalation as e:
                raise HarnessHalt(e.alarm)  # quality gate down -> fail closed

            if result["eligible"]:
                self.store.log(run_id, name, "stage", "pass", ok=True,
                               detail={"eligible": True, "judges": list(result["verdicts"].keys())})
                return result

            # HELD: a legitimate content flag -> route back to the writer.
            held = result["held"]
            if held_revisions >= budget:
                esc = Alarm(AlarmType.ESCALATE_HUMAN,
                            f"panel HELD the post after {held_revisions} writer revision(s); "
                            f"held criteria: {held}", name)
                self.store.log(run_id, name, "alarm", esc.type.value, ok=False, detail=esc.as_dict())
                raise HarnessHalt(esc)

            held_revisions += 1
            crit = Alarm(AlarmType.CONTENT_REJECTED,
                         f"panel HELD the post on {[h['criterion'] for h in held]}", name)
            self.store.log(run_id, name, "alarm", crit.type.value, ok=False, detail=crit.as_dict())
            feedback = "The review panel HELD your post. Revise to fix: " + "; ".join(
                f"[{h['criterion']}] {h['reason']}" for h in held)
            self.store.log(run_id, name, "revision", f"held->writer-revision-{held_revisions}",
                           ok=False, detail={"feedback": feedback})
            # Re-run the writer with the panel's critique, then loop to re-rehearse.
            self.run_content_stage(self.stage_by_name["write"], run_id, accumulated,
                                   initial_feedback=feedback)

    # -- Gate 3: Action, behind the human hold (dry-run by default) ----------
    def run_action_stage(self, stage: dict, run_id: str, accumulated: dict) -> dict:
        name = stage["name"]
        text = accumulated.get("draft", "")
        self.store.log(run_id, name, "stage", "start",
                       detail={"rendered": render_post({"text": text})})
        gate = ActionGate(self.store, self.guardrails, approver=self.approver)
        try:
            record = gate.run(run_id, text)
        except HumanDeclined as e:
            raise HarnessHalt(e.alarm)
        self.store.log(run_id, name, "stage", "pass", ok=True,
                       detail={"post_id": record["post_id"], "mode": record["mode"]})
        return record

    # -- the run ------------------------------------------------------------
    def run(self, run_id: Optional[str] = None, replay_from: Optional[str] = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:8]
        self.last_run_id = run_id  # so the timeline renders even when a gate halts
        stages = self.mission["stages"]

        # Determine where to start (replay support).
        start = 0
        if replay_from:
            names = [s["name"] for s in stages]
            if replay_from not in names:
                raise HarnessHalt(Alarm(AlarmType.ESCALATE_HUMAN,
                                        f"replay stage '{replay_from}' not in mission", "replay"))
            start = names.index(replay_from)

        self.store.log(run_id, "mission", "run", "start", ok=None,
                       detail={"mission": self.mission.get("mission"), "path": self.mission_path,
                               "replay_from": replay_from})

        accumulated: dict = {}
        # Preload outputs of skipped (already-completed) stages from the store.
        for s in stages[:start]:
            out = self.store.load_output(run_id, s["name"])
            if out is None:
                raise HarnessHalt(Alarm(AlarmType.ESCALATE_HUMAN,
                                        f"cannot replay: no stored output for '{s['name']}'", "replay"))
            self._absorb(out, accumulated)
            self.store.log(run_id, s["name"], "replay", "loaded-from-store", ok=True,
                           detail={"note": "skipped; reused persisted output"})

        # Run the remaining stages, dispatching by type.
        for s in stages[start:]:
            stype = s.get("type", "content")
            if stype == "content":
                self.run_content_stage(s, run_id, accumulated)
            elif stype == "rehearsal":
                self.run_rehearsal_stage(s, run_id, accumulated)
            elif stype == "action":
                self.run_action_stage(s, run_id, accumulated)
            else:
                raise HarnessHalt(Alarm(AlarmType.ESCALATE_HUMAN,
                                        f"unknown stage type '{stype}'", s.get("name", "?")))

        self.store.log(run_id, "mission", "run", "complete", ok=True, detail={"run_id": run_id})
        return run_id


# ---------------------------------------------------------------------------
# Observability -- the NASA-style flight log. A PURE READ over the store, so a
# killed-and-reloaded run (or a replay) renders identically.
# ---------------------------------------------------------------------------
_SEV_STYLE = {"medium": "yellow", "high": "dark_orange", "critical": "bold red"}


def render_timeline(store: Store, run_id: str) -> None:
    events = store.events(run_id)
    mission_name = run_id
    for e in events:
        if e["kind"] == "run" and e["name"] == "start" and e["detail"]:
            mission_name = e["detail"].get("mission", run_id)
            break

    console.print()
    console.print(Panel.fit(
        Text.assemble(("MISSION CONTROL  ", "bold cyan"),
                      ("·  flight log\n", "cyan"),
                      (f"mission: {mission_name}\n", "white"),
                      (f"run id : {run_id}", "white")),
        border_style="cyan"))

    table = Table(show_lines=False, expand=True, header_style="bold")
    table.add_column("seq", width=4, justify="right")
    table.add_column("stage", width=12)
    table.add_column("event", width=22)
    table.add_column("status", width=8, justify="center")
    table.add_column("detail", overflow="fold")

    for i, e in enumerate(events, 1):
        stage = e["stage"]
        kind = e["kind"]
        name = e["name"] or ""
        detail = e["detail"] or {}

        if kind == "check":
            status = Text("GO", style="bold green") if e["ok"] else Text("NO-GO", style="bold red")
            ev = Text(f"checkpoint:{name}")
            det = _fmt_check(name, e["ok"], detail)
        elif kind == "alarm":
            sev = (detail or {}).get("severity", "high")
            status = Text("ALARM", style=_SEV_STYLE.get(sev, "red"))
            ev = Text(f"alarm:{name}", style=_SEV_STYLE.get(sev, "red"))
            det = Text.assemble((f"[{sev}] ", _SEV_STYLE.get(sev, "red")),
                                (detail.get("context", ""), ""),
                                ("\n  -> ", "dim"), (detail.get("recommended_action", ""), "italic dim"))
        elif kind == "revision":
            status = Text("RETRY", style="yellow")
            ev = Text(f"revision:{name}", style="yellow")
            det = Text("feedback sent back to worker", style="dim")
        elif kind == "replay":
            status = Text("REPLAY", style="magenta")
            ev = Text(f"replay:{name}", style="magenta")
            det = Text(detail.get("note", ""), style="dim")
        elif kind == "attack":
            survived = bool(e["ok"])
            status = Text("SURVIVED", style="green") if survived else Text("BREACH", style="bold red")
            ev = Text(f"attack:{name}", style="green" if survived else "bold red")
            det = (Text("resisted", style="green") if survived
                   else Text(f"LEAK -> {detail.get('leaked', '')}", style="red"))
        elif kind == "certificate":
            certified = bool(e["ok"])
            status = Text("CERTIFIED", style="bold green") if certified else Text("REFUSED", style="bold red")
            ev = Text(f"certificate:{name}", style="bold")
            det = Text(f"agent '{detail.get('agent')}' -> certified={certified} (policy={detail.get('policy')})",
                       style="green" if certified else "red")
        elif kind == "verdict":
            status = Text("PASS", style="green") if e["ok"] else Text("HELD", style="dark_orange")
            ev = Text(f"judge:{name}")
            det = Text(f"overall={detail.get('overall')}", style="dim")
        elif kind == "panel":
            status = Text("GO", style="bold green") if e["ok"] else Text("HOLD", style="bold dark_orange")
            ev = Text("panel:unanimous-consent", style="bold")
            if e["ok"]:
                det = Text(f"all judges passed: {detail.get('judges')}", style="green")
            else:
                det = Text(f"HELD: {detail.get('held')}", style="dark_orange")
        elif kind == "approval":
            status = Text("APPROVED", style="bold green") if e["ok"] else Text("DENIED", style="bold red")
            ev = Text("human:approval", style="bold")
            det = Text(f"mode={detail.get('mode')}", style="dim")
        elif kind == "post":
            status = Text("POSTED", style="bold green")
            ev = Text("action:post", style="bold green")
            det = Text.assemble((f"[{detail.get('mode')}] id={name}  ", "green"),
                                (f"\"{detail.get('rendered', {}).get('text', '')}\"", "white"))
        elif kind == "takedown":
            status = Text("REMOVED", style="magenta")
            ev = Text("action:takedown", style="magenta")
            det = Text(f"post {name} taken down ({detail.get('mode')})", style="magenta")
        elif kind == "gate":
            status = Text("·", style="cyan")
            ev = Text(f"gate:{name}", style="cyan")
            if name == "digital-twin":
                det = Text(f"byte-identical payload built, egress={detail.get('egress')}", style="cyan")
            else:
                det = Text(str({k: detail.get(k) for k in ("attacks", "forbidden_tools") if k in detail}), style="dim")
        elif kind == "stage":
            if name == "pass":
                status = Text("GO", style="bold green")
            elif name == "start":
                status = Text("·", style="dim")
            else:
                status = Text(name.upper()[:6], style="dim")
            ev = Text(f"stage:{name}")
            det = _fmt_stage(name, detail)
        elif kind == "run":
            status = Text("***", style="bold cyan")
            ev = Text(f"mission:{name}", style="bold cyan")
            det = Text(str(detail.get("mission", detail.get("run_id", ""))), style="cyan")
        else:
            status = Text("·", style="dim")
            ev = Text(f"{kind}:{name}")
            det = Text(str(detail), style="dim")

        table.add_row(str(i), stage, ev, status, det)

    console.print(table)
    _render_telemetry(events)


def _fmt_check(name: str, ok: bool, detail: dict) -> Text:
    if name == "banned_claims" and not ok:
        return Text(f"banned phrase(s): {detail.get('matches')}", style="red")
    if name == "grounding" and not ok:
        offs = detail.get("offenders", [])
        first = offs[0]["sentence"] if offs else ""
        return Text(f"{len(offs)} ungrounded sentence(s); e.g. \"{first}\"", style="red")
    if name == "schema" and not ok:
        return Text(str(detail.get("errors", ""))[:160], style="red")
    if name == "meta_check" and not ok:
        return Text(f"judge faulty: {detail.get('problems')}", style="red")
    if name in ("margin", "arithmetic") and not ok:
        return Text(str(detail), style="red")
    return Text("ok", style="green") if ok else Text(str(detail)[:160], style="red")


def _fmt_stage(name: str, detail: dict) -> Text:
    if name == "start":
        if "worker" in detail:
            return Text(f"worker = {detail['worker']}", style="dim")
        if "gate" in detail:
            return Text(f"gate = {detail['gate']}", style="dim")
        return Text("staged for human hold", style="dim")
    if name == "pass":
        if detail.get("attempt") is not None:
            return Text(f"attempt {detail['attempt']} · {detail.get('seconds')}s · {detail.get('tokens')} tok",
                        style="green")
        if "post_id" in detail:
            return Text(f"posted {detail['post_id']} ({detail.get('mode')})", style="green")
        if "eligible" in detail:
            return Text(f"publish-eligible · judges={detail.get('judges')}", style="green")
        return Text("ok", style="green")
    if name == "pending":
        return Text(detail.get("note", ""), style="dim italic")
    return Text(str(detail), style="dim")


def _render_telemetry(events: list[dict]) -> None:
    rows = {}
    for e in events:
        if e["kind"] == "stage" and e["name"] == "pass" and e["detail"]:
            d = e["detail"]
            if d.get("attempt") is None:
                continue  # only content stages carry per-attempt telemetry
            rows[e["stage"]] = (d.get("attempt"), d.get("seconds"), d.get("tokens"))
    if not rows:
        return
    t = Table(title="per-stage telemetry", title_style="bold dim", expand=False, header_style="dim")
    t.add_column("stage")
    t.add_column("attempts", justify="right")
    t.add_column("seconds", justify="right")
    t.add_column("tokens", justify="right")
    for stage, (att, secs, tok) in rows.items():
        t.add_row(stage, str(att), str(secs), str(tok))
    console.print(t)


# ---------------------------------------------------------------------------
# Human-hold approvers
# ---------------------------------------------------------------------------
def _cli_approver(run_id: str, rendered: dict) -> bool:
    """Interactive human hold. Shows the exact post and asks for approval.
    A non-interactive stdin (no TTY) fails closed -- it does NOT post."""
    console.print(Panel.fit(
        Text.assemble(("HUMAN HOLD  ", "bold yellow"), ("Gate 3 / Action\n\n", "yellow"),
                      (rendered.get("text", ""), "white"),
                      (f"\n\n{rendered.get('char_count', 0)}/280 chars", "dim")),
        title="staged post (dry-run unless DRY_RUN=0 + creds)", border_style="yellow"))
    try:
        ans = input("Approve this post? [y/N] ").strip().lower()
    except EOFError:
        console.print("[dim]no interactive input -- treating as NOT approved (fail closed)[/]")
        return False
    return ans in ("y", "yes")


def auto_approver(run_id: str, rendered: dict) -> bool:
    """Non-interactive auto-approval for tests and `--yes`. The approval is still
    recorded in the store, satisfying the human-hold rule."""
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Mission Control -- the harness.")
    p.add_argument("--mission", default=os.path.join(BASE_DIR, "missions", "launch.yaml"),
                   help="path to the mission YAML")
    p.add_argument("--db", default=os.path.join(BASE_DIR, "mission.db"),
                   help="path to the SQLite store")
    p.add_argument("--real", action="store_true", help="use real models/agents (needs API keys)")
    p.add_argument("--faulty-grader", action="store_true", help="demo: a broken judge is caught by meta_check")
    p.add_argument("--reject-demo", action="store_true", help="demo: a sketchy agent fails Admission")
    p.add_argument("--block-demo", action="store_true",
                   help="demo: an unsupported medical claim is HELD at Rehearsal and never posts")
    p.add_argument("--full-panel", action="store_true",
                   help="show the full declared judge roster (Anthropic + OpenAI + the NVIDIA NIM bunch) as mock judges")
    p.add_argument("--replay-from", default=None, help="resume a saved run from this stage")
    p.add_argument("--run", default=None, help="run id to replay")
    p.add_argument("--yes", action="store_true", help="auto-approve the human hold (non-interactive)")
    p.add_argument("--verify-x", action="store_true",
                   help="read-only check of your X credentials (GET /2/users/me; never posts)")
    args = p.parse_args(argv)

    if args.verify_x:
        from gates.action import verify_x_credentials

        res = verify_x_credentials()
        if res["ok"]:
            console.print(f"[bold green]X credentials OK[/] — authenticated as "
                          f"@{res['username']} ({res['name']}, id {res['id']})")
            console.print("[dim]Identity confirmed. To actually post you still need the app set "
                          "to Read+Write, DRY_RUN=0, and a human approval at Gate 3.[/]")
            return 0
        console.print(f"[bold red]X credential check failed[/] — {res.get('reason')}")
        if res.get("missing"):
            console.print(f"  missing: {res['missing']}")
        if res.get("status"):
            console.print(f"  HTTP {res['status']}: {res.get('body', '')}")
        if res.get("hint"):
            console.print(f"  [dim]{res['hint']}[/]")
        return 1

    store = Store(args.db)
    approver = auto_approver if args.yes else _cli_approver
    h = Harness(args.mission, store, real=args.real,
                faulty_grader=args.faulty_grader, reject_demo=args.reject_demo,
                block_demo=args.block_demo, full_panel=args.full_panel, approver=approver)

    if args.replay_from and not args.run:
        console.print("[red]--replay-from requires --run <id>[/]")
        return 2

    code = 0
    run_id = args.run
    try:
        run_id = h.run(run_id=args.run, replay_from=args.replay_from)
    except HarnessHalt as halt:
        run_id = run_id or getattr(h, "last_run_id", None)
        console.print(f"[bold red]HALT[/] {halt.alarm.type.value}: {halt.alarm.context}")
        console.print(f"[dim]recommended action:[/] {halt.alarm.recommended_action}")
        code = 1
    finally:
        if run_id:
            render_timeline(store, run_id)
        store.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
