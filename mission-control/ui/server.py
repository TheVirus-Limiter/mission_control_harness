"""The Mission Control dashboard (FastAPI).

It is a PURE READ over the same SQLite store the engine writes to -- it runs no
pipeline and judges nothing. It assembles three views from the persisted events
and outputs:

  1. the live Mission Timeline (the same flight log as the terminal);
  2. a simulated X interface rendering the post exactly as it would appear, with
     one reviewer card per judge (model name, PASS/HELD, the one-line reason) --
     no fake likes, no scores;
  3. a gauntlet visualisation of the Admission run, one obstacle per real attack
     class, a fall being a real canary leak with the actual leaked output shown.

The animation in the frontend is a skin over this real pass/fail data.

There is one optional write: a dashboard human-hold approval that records the
approval and performs the dry-run post -- the same governed path the CLI uses.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid

# Make the project root importable whether launched as `uvicorn ui.server:app`
# from mission-control/ or as a module from elsewhere.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gates.action import DryRunXClient, build_x_payload, render_post
from materials import Store

DB_PATH = os.environ.get("MISSION_DB", os.path.join(BASE_DIR, "mission.db"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MISSIONS_DIR = os.path.join(BASE_DIR, "missions")

app = FastAPI(title="Mission Control")


def store() -> Store:
    return Store(DB_PATH)


# ---------------------------------------------------------------------------
# View assembly (all pure reads over the store)
# ---------------------------------------------------------------------------
_SEV = {"medium": "med", "high": "high", "critical": "crit"}


def _label(e: dict) -> dict:
    """Turn a raw event into a display row for the timeline."""
    kind, name, ok, detail = e["kind"], e["name"] or "", e["ok"], e["detail"] or {}
    row = {"stage": e["stage"], "kind": kind, "name": name, "ok": ok, "ts": e["ts"],
           "status": "", "tone": "muted", "text": "", "meta": ""}

    if kind == "check":
        row["status"] = "GO" if ok else "NO-GO"
        row["tone"] = "go" if ok else "nogo"
        row["text"] = f"checkpoint · {name}"
    elif kind == "alarm":
        sev = detail.get("severity", "high")
        row["status"] = "ALARM"
        row["tone"] = {"critical": "crit", "high": "high", "medium": "med"}.get(sev, "high")
        row["text"] = f"{detail.get('type', name)} — {detail.get('context', '')}"
    elif kind == "attack":
        row["status"] = "SURVIVED" if ok else "BREACH"
        row["tone"] = "go" if ok else "nogo"
        row["text"] = f"attack · {name}" + ("" if ok else f" — LEAK: {detail.get('leaked', '')}")
    elif kind == "certificate":
        row["status"] = "CERTIFIED" if ok else "REFUSED"
        row["tone"] = "go" if ok else "nogo"
        row["text"] = f"certificate · {detail.get('agent', name)}"
    elif kind == "verdict":
        row["status"] = "PASS" if ok else "HELD"
        row["tone"] = "go" if ok else "held"
        row["text"] = f"judge · {name}"
    elif kind == "panel":
        row["status"] = "GO" if ok else "HOLD"
        row["tone"] = "go" if ok else "held"
        row["text"] = "panel · unanimous consent"
    elif kind == "approval":
        row["status"] = "APPROVED" if ok else "DENIED"
        row["tone"] = "go" if ok else "nogo"
        row["text"] = "human · approval"
    elif kind == "post":
        row["status"] = "POSTED"
        row["tone"] = "go"
        row["text"] = f"action · post ({detail.get('mode')})"
    elif kind == "takedown":
        row["status"] = "REMOVED"
        row["tone"] = "held"
        row["text"] = f"action · takedown ({detail.get('mode')})"
    elif kind == "revision":
        row["status"] = "RETRY"
        row["tone"] = "med"
        fb = (detail.get("feedback") or "").replace("\n", " ")
        row["text"] = f"revision · {name}"
        row["meta"] = (fb[:120] + "…") if len(fb) > 120 else fb
    elif kind == "replay":
        row["status"] = "REPLAY"
        row["tone"] = "accent"
        row["text"] = f"replay · {e['stage']} reused from store"
    elif kind == "gate":
        row["tone"] = "accent"
        if name == "digital-twin":
            row["status"] = "TWIN"
            row["text"] = "digital-twin · byte-identical payload"
            row["meta"] = f"egress {detail.get('egress')} · {detail.get('byte_len')} bytes"
        else:
            row["status"] = "PROBE"
            row["text"] = f"admission · {name}"
    elif kind == "stage":
        row["status"] = {"pass": "GO", "start": "·"}.get(name, name.upper())
        row["tone"] = "go" if name == "pass" else "muted"
        row["text"] = f"stage · {e['stage']} {name}"
        if name == "pass" and detail.get("attempt") is not None:
            row["meta"] = (f"attempt {detail.get('attempt')} · {detail.get('seconds')}s "
                           f"· {detail.get('tokens')} tok")
    elif kind == "run":
        row["status"] = "***"
        row["tone"] = "accent"
        row["text"] = f"mission · {name}"
    elif kind == "info":
        row["status"] = "INFO"
        row["text"] = f"{e['stage']} · {name}"
    else:
        row["text"] = f"{kind} · {name}"
    return row


def _assemble(st: Store, run_id: str) -> dict:
    events = st.events(run_id)
    if not events:
        raise HTTPException(404, f"no such run: {run_id}")

    mission = run_id
    for e in events:
        if e["kind"] == "run" and e["name"] == "start" and e["detail"]:
            mission = e["detail"].get("mission", run_id)
            break

    # status
    complete = any(e["kind"] == "run" and e["name"] == "complete" for e in events)
    posted = any(e["kind"] == "post" for e in events)
    awaiting = any(e["kind"] == "alarm" and e["name"] == "AWAITING_HUMAN" for e in events)
    approved = any(e["kind"] == "approval" and e["ok"] for e in events)
    halted = any(e["kind"] == "alarm" and e["name"] in ("CERTIFICATION_FAILED", "ESCALATE_HUMAN")
                 for e in events) and not complete
    if posted:
        status = "posted (dry-run)" if not _is_live(events) else "posted (live)"
    elif halted:
        status = "halted"
    elif awaiting and not approved:
        status = "awaiting human"
    else:
        status = "complete" if complete else "running"

    # alarms
    alarms = [e["detail"] for e in events if e["kind"] == "alarm" and e["detail"]]

    # gauntlet from certificate events
    gauntlet = []
    for e in events:
        if e["kind"] == "certificate" and e["detail"]:
            d = e["detail"]
            gauntlet.append({
                "agent": d.get("agent"),
                "certified": d.get("certified"),
                "policy": d.get("policy"),
                "canary": d.get("canary_fingerprint"),
                "attacks": [
                    {"attack": a.get("attack"), "survived": a.get("survived"),
                     "leaked": a.get("evidence")}
                    for a in d.get("attacks", [])
                ],
            })

    # the digital-twin proof: egress disabled + byte length, byte-identical to live
    rehearsal_proof = None
    for e in events:
        if e["kind"] == "gate" and e["name"] == "digital-twin" and e["detail"]:
            rehearsal_proof = {"egress": e["detail"].get("egress"),
                               "byte_len": e["detail"].get("byte_len")}

    # counts for the header readout
    agents_certified = sum(1 for g in gauntlet if g["certified"])
    open_alarms = sum(1 for a in alarms if a.get("severity") in ("high", "critical"))
    taken_down = any(e["kind"] == "takedown" for e in events)

    # panel + post from outputs
    panel = _panel_view(st, run_id)
    post = _post_view(st, run_id, events)
    post_id = next((e["name"] for e in events if e["kind"] == "post"), None)

    timeline = [{"seq": i + 1, **_label(e)} for i, e in enumerate(events)]
    return {"run_id": run_id, "mission": mission, "status": status,
            "posted": posted, "awaiting": awaiting and not approved,
            "halted": halted, "can_takedown": bool(posted and not taken_down),
            "taken_down": taken_down, "post_id": post_id,
            "agents_certified": agents_certified, "open_alarms": open_alarms,
            "rehearsal_proof": rehearsal_proof,
            "timeline": timeline, "alarms": alarms, "gauntlet": gauntlet,
            "panel": panel, "post": post}


def _is_live(events) -> bool:
    for e in events:
        if e["kind"] == "post" and e["detail"]:
            return bool(e["detail"].get("live"))
    return False


def _panel_view(st: Store, run_id: str) -> dict:
    out = st.load_output(run_id, "rehearsal")
    if not out:
        return {"present": False, "eligible": None, "judges": [], "rubric": []}
    held_by_judge: dict[str, list] = {}
    for h in out.get("held", []):
        held_by_judge.setdefault(h["judge"], []).append(h)
    judges = []
    for name, verdict in out.get("verdicts", {}).items():
        holds = held_by_judge.get(name, [])
        passed = not holds
        comment = (verdict.get("comment") or "").strip()
        if holds:
            reason = "; ".join(f"{h['criterion']}: {h['reason']}" for h in holds)
            if not comment:
                comment = "Holding this — " + reason
            judges.append({"name": name, "verdict": "HELD", "pass": False, "reason": reason,
                           "comment": comment, "criteria": verdict.get("criteria", {})})
        else:
            if not comment:
                comment = "Looks good to me — clear and on-brand."
            judges.append({"name": name, "verdict": "PASS", "pass": True,
                           "reason": "all criteria passed", "comment": comment,
                           "criteria": verdict.get("criteria", {})})
    return {"present": True, "eligible": out.get("eligible"),
            "judges": judges, "held": out.get("held", []),
            "rubric": out.get("rubric", [])}


def _post_view(st: Store, run_id: str, events) -> dict:
    # Prefer the actual post event (what was sent / dry-run sent).
    for e in events:
        if e["kind"] == "post" and e["detail"]:
            d = e["detail"]
            r = d.get("rendered") or render_post(d.get("payload", {}))
            return {"present": True, "posted": True, "mode": d.get("mode"),
                    "post_id": d.get("post_id"), "live": d.get("live", False), **r}
    # Otherwise show the staged (rehearsed) post, not yet sent.
    reh = st.load_output(run_id, "rehearsal")
    if reh and reh.get("rendered"):
        return {"present": True, "posted": False, "mode": "staged",
                "post_id": None, "live": False, **reh["rendered"]}
    draft = st.load_output(run_id, "write")
    if draft and draft.get("text"):
        return {"present": True, "posted": False, "mode": "draft", "post_id": None,
                "live": False, **render_post(build_x_payload(draft["text"]))}
    return {"present": False}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/runs")
def api_runs():
    st = store()
    try:
        out = []
        for rid in st.runs():
            view = _assemble(st, rid)
            out.append({"run_id": rid, "mission": view["mission"], "status": view["status"]})
        return out
    finally:
        st.close()


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    st = store()
    try:
        return _assemble(st, run_id)
    finally:
        st.close()


@app.post("/api/runs/{run_id}/approve")
def api_approve(run_id: str):
    """Dashboard human-hold approval. Routes through the SAME ActionGate the CLI
    uses (one governed posting path), with a dashboard-interactive approver so it
    can perform a real post when DRY_RUN=0 + creds are present. Refuses if the
    panel is not publish-eligible or it already posted."""
    from guardrails import Guardrails
    from gates.action import ActionGate, render_post

    st = store()
    try:
        events = st.events(run_id)
        if not events:
            raise HTTPException(404, "no such run")
        if any(e["kind"] == "post" for e in events):
            return JSONResponse({"ok": False, "reason": "already posted"}, status_code=409)
        reh = st.load_output(run_id, "rehearsal")
        if not reh or not reh.get("eligible"):
            return JSONResponse({"ok": False, "reason": "panel did not pass — not publish-eligible"},
                                status_code=409)
        text = (reh.get("rendered") or {}).get("text") or reh.get("payload", {}).get("text", "")
        handle = (reh.get("rendered") or {}).get("author", "@yourbrand")

        # A dashboard click IS an interactive human approval.
        def approver(rid, rendered):
            return True
        approver.interactive = True  # type: ignore[attr-defined]

        gate = ActionGate(st, Guardrails(human_hold_required=True), approver=approver, handle=handle)
        record = gate.run(run_id, text)
        st.log(run_id, "action", "stage", "pass", ok=True,
               detail={"post_id": record["post_id"], "mode": record["mode"]})
        return {"ok": True, "post_id": record["post_id"], "mode": record["mode"]}
    finally:
        st.close()


@app.post("/api/runs/{run_id}/takedown")
def api_takedown(run_id: str):
    """Roll back a post (the takedown story). Records a takedown event (and calls
    the real delete endpoint only in live mode)."""
    from guardrails import Guardrails
    from gates.action import ActionGate

    st = store()
    try:
        events = st.events(run_id)
        post = next((e for e in events if e["kind"] == "post"), None)
        if not post:
            return JSONResponse({"ok": False, "reason": "nothing posted to take down"}, status_code=409)
        if any(e["kind"] == "takedown" for e in events):
            return JSONResponse({"ok": False, "reason": "already taken down"}, status_code=409)
        gate = ActionGate(st, Guardrails(), approver=lambda r, x: True)
        rec = gate.takedown(run_id, post["name"])
        return {"ok": True, "post_id": rec["post_id"]}
    finally:
        st.close()


# ---------------------------------------------------------------------------
# Mission control: list presets/missions and LAUNCH a run from the dashboard.
# Launches are always non-interactive -> dry-run (they can never post live).
# ---------------------------------------------------------------------------
@app.get("/api/presets")
def api_presets():
    from models.judges import PRESETS

    return [{"key": p.key, "label": p.name, "vendor": p.vendor, "provider": p.provider,
             "model": p.model, "available": bool(os.environ.get(p.env_key))}
            for p in PRESETS]


@app.get("/api/missions")
def api_missions():
    import yaml

    out = []
    for fn in sorted(os.listdir(MISSIONS_DIR)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(MISSIONS_DIR, fn), encoding="utf-8") as f:
                m = yaml.safe_load(f)
            out.append({"key": fn.rsplit(".", 1)[0], "name": m.get("mission", fn),
                        "description": m.get("description", "")})
        except Exception:
            continue
    return out


_RUNS_LOCK = threading.Lock()


@app.post("/api/launch")
def api_launch(body: dict = Body(default={})):
    """Start a run in the background (non-interactive -> dry-run). Returns its
    run_id immediately; the dashboard then polls it live like any other run."""
    mission_key = (body.get("mission") or "launch").replace("..", "").replace("/", "")
    mission_path = os.path.join(MISSIONS_DIR, mission_key + ".yaml")
    if not os.path.isfile(mission_path):
        raise HTTPException(404, f"no such mission: {mission_key}")

    preset = body.get("preset") or None
    scenario = body.get("scenario") or "normal"   # normal|reject|block|faulty|full
    real = bool(body.get("real")) or bool(preset)
    flags = {"reject_demo": scenario == "reject", "block_demo": scenario == "block",
             "faulty_grader": scenario == "faulty", "full_panel": scenario == "full"}
    run_id = uuid.uuid4().hex[:8]

    def worker():
        from harness import Harness, auto_approver  # lazy: heavy import

        os.environ["DRY_RUN"] = "1"  # launches never post live
        st = Store(DB_PATH)
        try:
            Harness(mission_path, st, real=real, preset=preset, approver=auto_approver,
                    **flags).run(run_id=run_id)
        except Exception as e:
            try:
                st.log(run_id, "mission", "run", "error", ok=False, detail={"error": str(e)})
            except Exception:
                pass
        finally:
            st.close()

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id, "mission": mission_key, "preset": preset, "scenario": scenario}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
