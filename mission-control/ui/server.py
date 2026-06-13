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

# Make the project root importable whether launched as `uvicorn ui.server:app`
# from mission-control/ or as a module from elsewhere.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gates.action import DryRunXClient, build_x_payload, render_post
from materials import Store

DB_PATH = os.environ.get("MISSION_DB", os.path.join(BASE_DIR, "mission.db"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

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
    row = {"stage": e["stage"], "kind": kind, "name": name, "ok": ok,
           "status": "", "tone": "muted", "text": ""}

    if kind == "check":
        row["status"] = "GO" if ok else "NO-GO"
        row["tone"] = "go" if ok else "nogo"
        row["text"] = f"checkpoint · {name}"
    elif kind == "alarm":
        sev = detail.get("severity", "high")
        row["status"] = "ALARM"
        row["tone"] = "crit" if sev == "critical" else ("high" if sev == "high" else "med")
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
    elif kind == "revision":
        row["status"] = "RETRY"
        row["tone"] = "med"
        row["text"] = f"revision · {name}"
    elif kind == "replay":
        row["status"] = "REPLAY"
        row["tone"] = "muted"
        row["text"] = f"replay · {e['stage']}"
    elif kind == "stage":
        row["status"] = {"pass": "GO", "start": "·"}.get(name, name.upper())
        row["tone"] = "go" if name == "pass" else "muted"
        row["text"] = f"stage · {name}"
    elif kind == "run":
        row["status"] = "***"
        row["tone"] = "accent"
        row["text"] = f"mission · {name}"
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
                "attacks": [
                    {"attack": a.get("attack"), "survived": a.get("survived"),
                     "leaked": a.get("evidence")}
                    for a in d.get("attacks", [])
                ],
            })

    # panel + post from outputs
    panel = _panel_view(st, run_id)
    post = _post_view(st, run_id, events)

    timeline = [{"seq": i + 1, **_label(e)} for i, e in enumerate(events)]
    return {"run_id": run_id, "mission": mission, "status": status,
            "posted": posted, "awaiting": awaiting and not approved,
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
        if holds:
            reason = "; ".join(f"{h['criterion']}: {h['reason']}" for h in holds)
            judges.append({"name": name, "verdict": "HELD", "reason": reason,
                           "criteria": verdict.get("criteria", {})})
        else:
            judges.append({"name": name, "verdict": "PASS",
                           "reason": "all criteria passed",
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
    """Dashboard human-hold approval: records the approval and performs the
    dry-run post -- the same governed path as the CLI. Refuses if the panel
    is not publish-eligible or if it already posted."""
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
        text = reh.get("payload", {}).get("text", "")
        st.log(run_id, "action", "approval", "human(dashboard)", ok=True,
               detail={"approved": True, "mode": "dry_run", "via": "dashboard"})
        record = DryRunXClient(st).post(run_id, build_x_payload(text))
        st.log(run_id, "action", "stage", "pass", ok=True,
               detail={"post_id": record["post_id"], "mode": record["mode"]})
        return {"ok": True, "post_id": record["post_id"], "mode": record["mode"]}
    finally:
        st.close()


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
