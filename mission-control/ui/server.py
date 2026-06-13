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

import json
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gates.action import DryRunXClient, build_x_payload, render_post
from materials import Store

# Load mission-control/.env so the SERVER process has the API keys (Anthropic /
# OpenAI / NVIDIA / Tavily) and X credentials. Without this the dashboard would
# silently fall back to mock workers for every run. Never overrides vars already
# set in the real environment (e.g. on Render).
try:
    from harness import load_dotenv
    load_dotenv()
except Exception:
    pass

DB_PATH = os.environ.get("MISSION_DB", os.path.join(BASE_DIR, "mission.db"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MISSIONS_DIR = os.path.join(BASE_DIR, "missions")

app = FastAPI(title="Mission Control")
# Local dev dashboard -- allow any origin so the static preview can reach a
# running server even from a file:// / cross-origin context.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


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
    mission_path = None
    for e in events:
        if e["kind"] == "run" and e["name"] == "start" and e["detail"]:
            mission = e["detail"].get("mission", run_id)
            mission_path = e["detail"].get("path")
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

    # OBSERVABILITY: what it searched + where it looked.
    research = None
    rout = st.load_output(run_id, "research")
    if rout:
        research = {"topic": rout.get("topic"), "query": rout.get("query"),
                    "engine": rout.get("engine", "model"),
                    "facts": rout.get("facts", {}), "sources": rout.get("sources", {})}

    # OBSERVABILITY: what the model wrote each attempt + where it got flagged.
    drafts = [{"stage": e["stage"], "attempt": (e["detail"] or {}).get("attempt"),
               "worker": (e["detail"] or {}).get("worker"), "ok": e["ok"],
               "text": (e["detail"] or {}).get("text", ""),
               "flags": (e["detail"] or {}).get("flags", [])}
              for e in events if e["kind"] == "draft" and e["detail"]]

    # REVISION DIFFS: behaviour-change-on-feedback made visible. Between
    # consecutive draft attempts of a stage, compute the word-level change and the
    # checkpoint feedback that caused it (the failing draft's own flags).
    by_stage: dict[str, list] = {}
    for d in drafts:
        by_stage.setdefault(d["stage"], []).append(d)
    revisions = []
    for stage, ds in by_stage.items():
        ds = sorted(ds, key=lambda x: x["attempt"] or 0)
        for a, b in zip(ds, ds[1:]):
            diff = _word_diff(a["text"], b["text"])
            revisions.append({"stage": stage, "from": a["attempt"], "to": b["attempt"],
                              "ops": diff["ops"], "removed": diff["removed"],
                              "added": diff["added"], "feedback": a["flags"]})

    # panel + post from outputs
    panel = _panel_view(st, run_id)
    post = _post_view(st, run_id, events)
    post_id = next((e["name"] for e in events if e["kind"] == "post"), None)

    # ITEM 3: standing guardrails a human saved for this mission + any free-text
    # corrections recorded on this run (a pure read; never a model update).
    from harness import load_learned_guidance
    learned_guidance = load_learned_guidance(mission_path) if mission_path else []
    corrections = [{"name": e["name"], **(e["detail"] or {})}
                   for e in events if e["kind"] == "revision"
                   and e["name"] in ("human-reject", "human-correction")]

    # ITEM 4: cost + latency meter -- a pure read over the telemetry above.
    from cost import meter
    cost = meter(events)

    timeline = [{"seq": i + 1, **_label(e)} for i, e in enumerate(events)]
    return {"run_id": run_id, "mission": mission, "status": status,
            "posted": posted, "awaiting": awaiting and not approved,
            "halted": halted, "can_takedown": bool(posted and not taken_down),
            "taken_down": taken_down, "post_id": post_id,
            "agents_certified": agents_certified, "open_alarms": open_alarms,
            "rehearsal_proof": rehearsal_proof, "research": research, "drafts": drafts,
            "revisions": revisions, "learned_guidance": learned_guidance,
            "corrections": corrections, "cost": cost,
            "timeline": timeline, "alarms": alarms, "gauntlet": gauntlet,
            "panel": panel, "post": post}


def _word_diff(a: str, b: str) -> dict:
    """Word-level diff for the revision view: an ordered op stream plus the lists
    of removed / added words. Pure stdlib (difflib)."""
    import difflib
    at, bt = (a or "").split(), (b or "").split()
    sm = difflib.SequenceMatcher(a=at, b=bt, autojunk=False)
    ops, removed, added = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ops.append({"t": "eq", "text": " ".join(at[i1:i2])})
        else:
            if i2 > i1:
                seg = at[i1:i2]
                ops.append({"t": "del", "text": " ".join(seg)})
                removed += seg
            if j2 > j1:
                seg = bt[j1:j2]
                ops.append({"t": "ins", "text": " ".join(seg)})
                added += seg
    return {"ops": ops, "removed": removed, "added": added}


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
    profiles = out.get("judge_profiles", {})
    assigned = out.get("assigned", {})
    judges = []
    for name, verdict in out.get("verdicts", {}).items():
        holds = held_by_judge.get(name, [])
        comment = (verdict.get("comment") or "").strip()
        if holds:
            reason = "; ".join(f"{h['criterion']}: {h['reason']}" for h in holds)
            judges.append({"name": name, "verdict": "HELD", "pass": False, "reason": reason,
                           "comment": comment or ("Holding this — " + reason),
                           "criteria": verdict.get("criteria", {}),
                           "profile": profiles.get(name, "deep"), "covers": assigned.get(name, [])})
        else:
            judges.append({"name": name, "verdict": "PASS", "pass": True,
                           "reason": "all assigned criteria passed",
                           "comment": comment or "Looks good to me — clear and on-brand.",
                           "criteria": verdict.get("criteria", {}),
                           "profile": profiles.get(name, "deep"), "covers": assigned.get(name, [])})
    # For the blocked-post CLIMAX: the exact offending sentence each judge cited,
    # so the frozen post can highlight the lie inline.
    verdicts = out.get("verdicts", {})
    held_spans = []
    for h in out.get("held", []):
        span = (verdicts.get(h["judge"], {}).get("citations", {}) or {}).get(h["criterion"], "")
        held_spans.append({"criterion": h["criterion"], "judge": h["judge"],
                           "tier": h.get("tier", "deep"), "reason": h.get("reason", ""),
                           "span": span})
    return {"present": True, "eligible": out.get("eligible"),
            "judges": judges, "held": out.get("held", []), "held_spans": held_spans,
            "rubric": out.get("rubric", []), "criteria_outcomes": out.get("criteria_outcomes", {})}


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
# Audit report: every governed run produces a complete, exportable audit trail.
# A PURE READ over the store (+ the declared mission file for input/guardrails).
# ---------------------------------------------------------------------------
def _report(st: Store, run_id: str) -> dict:
    import datetime
    import yaml

    events = st.events(run_id)
    if not events:
        raise HTTPException(404, f"no such run: {run_id}")
    view = _assemble(st, run_id)

    cfg, mission_path = {}, None
    for e in events:
        if e["kind"] == "run" and e["name"] == "start" and (e["detail"] or {}).get("path"):
            mission_path = e["detail"]["path"]
            try:
                with open(mission_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                cfg = {}
            break
    from harness import load_learned_guidance
    learned = load_learned_guidance(mission_path) if mission_path else []
    if learned:
        cfg.setdefault("guardrails", {})["learned_guidance"] = learned

    checkpoints = [{"stage": e["stage"], "name": e["name"], "ok": e["ok"], "evidence": e["detail"]}
                   for e in events if e["kind"] == "check"]
    certificates = [e["detail"] for e in events if e["kind"] == "certificate" and e["detail"]]
    approvals = [e["detail"] for e in events if e["kind"] == "approval" and e["detail"]]
    # ITEM 3: a human reviewer's free-text corrections (the HOLD that triggered a
    # re-run, and any correction wired into THIS run's writer revise loop).
    corrections = [{"name": e["name"], **(e["detail"] or {})}
                   for e in events if e["kind"] == "revision"
                   and e["name"] in ("human-reject", "human-correction")]
    return {
        "run_id": run_id,
        "mission": view["mission"],
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input": cfg.get("input", {}),
        "declared_guardrails": cfg.get("guardrails", {}),
        "criterion_profiles": cfg.get("criterion_profiles", {}),
        "outcome": {"status": view["status"], "posted": view["posted"], "post": view["post"]},
        "admission": {"agents_certified": view["agents_certified"], "certificates": certificates,
                      "gauntlet": view["gauntlet"]},
        "research": view["research"],
        "drafts": view["drafts"],
        "revisions": view.get("revisions", []),
        "checkpoints": checkpoints,
        "rehearsal_proof": view["rehearsal_proof"],
        "panel": view["panel"],
        "human_approval": approvals,
        "human_corrections": corrections,
        "alarms": view["alarms"],
        "cost": view.get("cost"),
        "timeline": view["timeline"],
    }


def _h(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _report_html(r: dict) -> str:
    def kv(d):
        return "".join(f"<tr><td class=k>{_h(k)}</td><td>{_h(v)}</td></tr>" for k, v in (d or {}).items())
    # admission
    certrows = ""
    for c in r["admission"]["certificates"]:
        att = "".join(
            f"<li class='{'ok' if a.get('survived') else 'no'}'>{_h(a.get('attack'))}: "
            f"{'survived' if a.get('survived') else 'BREACH — ' + _h(a.get('evidence'))}</li>"
            for a in c.get("attacks", []))
        certrows += (f"<tr><td>{_h(c.get('agent'))}</td>"
                     f"<td class='{'ok' if c.get('certified') else 'no'}'>"
                     f"{'CERTIFIED' if c.get('certified') else 'REFUSED'}</td>"
                     f"<td><ul class=att>{att}</ul></td></tr>")
    # checkpoints
    chkrows = "".join(
        f"<tr><td>{_h(c['stage'])}</td><td>{_h(c['name'])}</td>"
        f"<td class='{'ok' if c['ok'] else 'no'}'>{'GO' if c['ok'] else 'NO-GO'}</td>"
        f"<td class=ev>{_h(json.dumps(c['evidence'], default=str))[:300]}</td></tr>"
        for c in r["checkpoints"])
    # drafts
    draftblocks = ""
    for d in r["drafts"]:
        flags = "".join(f"<li>{_h(f.get('check'))}: {_h(f.get('reason'))}"
                        + (f" — “{_h(f.get('span'))}”" if f.get("span") else "") + "</li>"
                        for f in d.get("flags", []))
        draftblocks += (f"<div class=draft><b>Attempt #{_h(d.get('attempt'))}</b> "
                        f"<span class='{'ok' if d.get('ok') else 'no'}'>"
                        f"{'passed checks' if d.get('ok') else 'flagged'}</span>"
                        f"<p class=body>{_h(d.get('text'))}</p>"
                        + (f"<ul class=flags>{flags}</ul>" if flags else "") + "</div>")
    # panel
    p = r["panel"] or {}
    jrows = "".join(
        f"<tr><td>{_h(j.get('name'))}</td><td>{_h(j.get('profile'))}</td>"
        f"<td class='{'ok' if j.get('pass') else 'no'}'>{_h(j.get('verdict'))}</td>"
        f"<td>{_h(', '.join(j.get('covers', [])))}</td><td>{_h(j.get('comment'))}</td></tr>"
        for j in p.get("judges", []))
    crows = "".join(
        f"<tr><td>{_h(c)}</td><td>{_h(o.get('min_tier'))}</td>"
        f"<td class='{'ok' if o.get('passed') else 'no'}'>{'PASS' if o.get('passed') else 'HELD'}</td>"
        f"<td>{_h(', '.join(o.get('voters', [])))}</td></tr>"
        for c, o in (p.get("criteria_outcomes") or {}).items())
    # research / sources
    srcrows = ""
    if r["research"]:
        for k, v in (r["research"].get("facts") or {}).items():
            src = (r["research"].get("sources") or {}).get(k, "")
            srcrows += f"<tr><td class=k>[{_h(k)}]</td><td>{_h(v)}</td><td>{_h(src)}</td></tr>"
    alarmrows = "".join(
        f"<tr><td class=no>{_h(a.get('severity'))}</td><td>{_h(a.get('type'))}</td>"
        f"<td>{_h(a.get('context'))}</td><td>{_h(a.get('recommended_action'))}</td></tr>"
        for a in r["alarms"])
    apr = "".join(f"<tr>{kv(a)}</tr>" if isinstance(a, dict) else "" for a in r["human_approval"])
    corrrows = "".join(
        f"<tr><td>{'reviewer HOLD' if c.get('name') == 'human-reject' else 'applied to writer'}</td>"
        f"<td>{_h(c.get('correction') or c.get('feedback'))}</td>"
        f"<td class='{'ok' if c.get('saved_as_guardrail') else 'muted'}'>"
        f"{'saved as standing guardrail' if c.get('saved_as_guardrail') else ('not saved' if c.get('name') == 'human-reject' else 'this run only')}</td></tr>"
        for c in (r.get("human_corrections") or []))
    cost = r.get("cost") or {}
    costsec = ""
    if cost.get("present"):
        gaterows = "".join(
            f"<tr><td>{_h(g)}</td><td>{_h(v.get('tokens', 0))}</td>"
            f"<td>{_h(v.get('seconds', 0))}s</td><td>${v.get('cost_usd', 0):.4f}</td></tr>"
            for g, v in (cost.get("by_gate") or {}).items())
        modelrows = "".join(
            f"<tr><td>{_h(m.get('model'))}</td><td>{_h(m.get('tokens', 0))}</td>"
            f"<td>${m.get('price_per_1m', 0):.2f}/1M</td><td>${m.get('cost_usd', 0):.4f}</td></tr>"
            for m in (cost.get("by_model") or []))
        costsec = (
            "<h2>9. Cost &amp; latency <span class=muted style=font-weight:400>(estimate)</span></h2>"
            f"<p>Wall-clock <b>{_h(cost.get('wall_clock_s'))}s</b> · "
            f"total tokens <b>{_h(cost.get('total_tokens'))}</b> · "
            f"estimated cost <b>${cost.get('estimated_cost_usd', 0):.4f}</b> "
            f"<span class=muted>({_h(cost.get('price_basis'))})</span></p>"
            "<table><tr><td class=k>Gate</td><td class=k>Tokens</td><td class=k>Wall-time</td>"
            f"<td class=k>Est. cost</td></tr>{gaterows}</table>"
            + (f"<h3>By model</h3><table><tr><td class=k>Model</td><td class=k>Tokens</td>"
               f"<td class=k>Price</td><td class=k>Est. cost</td></tr>{modelrows}</table>"
               if modelrows else "<p class=muted>no metered model tokens (mock run)</p>"))
    out = r["outcome"]
    css = ("body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:24px auto;"
           "padding:0 18px;color:#16202c;line-height:1.5}h1{margin:0}h2{border-bottom:2px solid #e2e8f0;"
           "padding-bottom:6px;margin-top:30px;font-size:18px}table{width:100%;border-collapse:collapse;"
           "margin:8px 0;font-size:13px}td{border:1px solid #e2e8f0;padding:6px 9px;vertical-align:top}"
           "td.k{font-weight:600;width:200px;background:#f7fafc}.ok{color:#15803d;font-weight:600}"
           ".no{color:#b91c1c;font-weight:600}.muted{color:#64748b}ul{margin:4px 0;padding-left:18px}"
           ".att li.no{color:#b91c1c}.draft{border:1px solid #e2e8f0;border-radius:8px;padding:10px;margin:8px 0}"
           ".draft .body{white-space:pre-wrap;background:#f8fafc;padding:8px;border-radius:6px}"
           "td.ev{font-family:monospace;font-size:11px;color:#475569}.badge{display:inline-block;padding:3px 12px;"
           "border-radius:6px;font-weight:700}.b-go{background:#dcfce7;color:#15803d}.b-no{background:#fee2e2;color:#b91c1c}"
           ".b-hold{background:#fef3c7;color:#b45309}@media print{h2{break-after:avoid}.draft{break-inside:avoid}}")
    ob = "b-go" if out["posted"] else ("b-no" if out["status"] == "halted" else "b-hold")
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>Audit Report — {_h(r['run_id'])}</title>
<style>{css}</style></head><body>
<h1>Mission Control — Governance Audit Report</h1>
<p class=muted>Run <b>{_h(r['run_id'])}</b> · {_h(r['mission'])} · generated {_h(r['generated_at'])}</p>
<p>Final outcome: <span class="badge {ob}">{_h(out['status'].upper())}</span></p>
<h2>1. Mission input &amp; declared rulebook</h2>
<table>{kv(r['input'])}</table>
<h3>Declared guardrails</h3><table>{kv(r['declared_guardrails'])}</table>
{'<h3>Criterion tiers</h3><table>'+kv(r['criterion_profiles'])+'</table>' if r['criterion_profiles'] else ''}
<h2>2. Admission — the gauntlet ({_h(r['admission']['agents_certified'])} certified)</h2>
<table><tr><td class=k>Agent</td><td class=k>Verdict</td><td class=k>Attacks</td></tr>{certrows}</table>
<h2>3. Research — what it searched, where it looked</h2>
{'<p class=muted>query: '+_h((r['research'] or {}).get('query'))+' · engine: '+_h((r['research'] or {}).get('engine'))+'</p><table><tr><td class=k>Fact</td><td class=k>Statement</td><td class=k>Source</td></tr>'+srcrows+'</table>' if r['research'] else '<p class=muted>no research recorded</p>'}
<h2>4. Drafts — what the model wrote, and where it was flagged</h2>
{draftblocks or '<p class=muted>no drafts</p>'}
<h2>5. Content checkpoints (deterministic)</h2>
<table><tr><td class=k>Stage</td><td class=k>Check</td><td class=k>Result</td><td class=k>Evidence</td></tr>{chkrows}</table>
<h2>6. Rehearsal — the digital twin &amp; the panel</h2>
{'<p class=muted>🔒 rehearsed with network egress '+_h((r['rehearsal_proof'] or {}).get('egress'))+' · '+_h((r['rehearsal_proof'] or {}).get('byte_len'))+' bytes byte-identical to live</p>' if r['rehearsal_proof'] else ''}
<p>Panel verdict: <b class="{'ok' if p.get('eligible') else 'no'}">{'UNANIMOUS CONSENT — publish-eligible' if p.get('eligible') else 'HELD'}</b></p>
<table><tr><td class=k>Judge</td><td class=k>Tier</td><td class=k>Verdict</td><td class=k>Votes on</td><td class=k>Comment</td></tr>{jrows}</table>
<h3>Per-criterion consent (unanimous within voters)</h3>
<table><tr><td class=k>Criterion</td><td class=k>Min tier</td><td class=k>Outcome</td><td class=k>Voters</td></tr>{crows}</table>
<h2>7. Human hold</h2><table>{apr or '<tr><td class=muted>no approval recorded</td></tr>'}</table>
{'<h2>7b. Reviewer corrections (declared — not model training)</h2><p class=muted>A human reviewer typed a free-text correction; it was fed to the writer&#39;s revise loop. Saving appends it to the mission&#39;s declared guardrails. No weights change.</p><table><tr><td class=k>Kind</td><td class=k>Correction</td><td class=k>Persistence</td></tr>'+corrrows+'</table>' if corrrows else ''}
<h2>8. Alarms</h2>
{'<table><tr><td class=k>Severity</td><td class=k>Type</td><td class=k>Context</td><td class=k>Recommended action</td></tr>'+alarmrows+'</table>' if alarmrows else '<p class=muted>no alarms — every gate clean</p>'}
{costsec}
<p class=muted style=margin-top:40px>Generated by Mission Control. This report is a pure read over the run's persisted audit trail.</p>
</body></html>"""


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


def _attack_stats(st: Store) -> dict:
    """Session-level Proving Ground scoreboard: a PURE READ across every run's
    persisted `attack` and `certificate` events. Counts the adversarial probes
    fired at agents, how many were survived, and how many agents were refused
    admission outright."""
    attacks = survived = breaches = refused = certified = 0
    agents_refused: set = set()
    for rid in st.runs():
        for e in st.events(rid):
            if e["kind"] == "attack":
                attacks += 1
                if (e["detail"] or {}).get("survived") or e["ok"]:
                    survived += 1
                else:
                    breaches += 1
            elif e["kind"] == "certificate":
                if e["ok"]:
                    certified += 1
                else:
                    refused += 1
                    agents_refused.add((e["detail"] or {}).get("agent") or e["name"])
    return {"attacks": attacks, "survived": survived, "breaches": breaches,
            "agents_refused": refused, "agents_certified": certified,
            "distinct_agents_refused": sorted(a for a in agents_refused if a),
            "survival_rate": round(survived / attacks, 4) if attacks else None}


@app.get("/api/attack-stats")
def api_attack_stats():
    """Session-wide attacks-survived headline (read-only)."""
    st = store()
    try:
        return _attack_stats(st)
    finally:
        st.close()


@app.get("/api/runs/{run_id}/report")
def api_report(run_id: str):
    """The full governance audit trail for a run, as portable JSON."""
    st = store()
    try:
        return _report(st, run_id)
    finally:
        st.close()


@app.get("/api/runs/{run_id}/report.html")
def api_report_html(run_id: str):
    """The same audit trail as a clean, self-contained, printable HTML report."""
    st = store()
    try:
        return HTMLResponse(_report_html(_report(st, run_id)))
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
def api_approve(run_id: str, body: dict = Body(default={})):
    """Dashboard human-hold approval. Routes through the SAME ActionGate the CLI
    uses (one governed posting path), with a dashboard-interactive approver. Default
    is a dry-run; pass `live: true` to publish for real to X (requires the X creds).
    Refuses if the panel is not publish-eligible or it already posted."""
    from guardrails import Guardrails
    from gates.action import ActionGate, would_post_live

    live = bool(body.get("live"))
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

        # Live posting is opt-in per click: set DRY_RUN for THIS request only (and
        # restore it) so select_client picks RealXClient only when the human asked
        # to go live AND the X creds are present. Default stays dry-run. The
        # _RUNS_LOCK serialises the DRY_RUN window so two concurrent approves can
        # never cross wires (a live request can't make a dry request post live).
        with _RUNS_LOCK:
            prev_dry = os.environ.get("DRY_RUN")
            os.environ["DRY_RUN"] = "0" if live else "1"
            try:
                if live and not would_post_live():
                    return JSONResponse({"ok": False, "reason": "live requested but X credentials are "
                                         "not configured — staying dry-run"}, status_code=409)
                gate = ActionGate(st, Guardrails(human_hold_required=True), approver=approver, handle=handle)
                record = gate.run(run_id, text)
            finally:
                if prev_dry is None:
                    os.environ.pop("DRY_RUN", None)
                else:
                    os.environ["DRY_RUN"] = prev_dry

        st.log(run_id, "action", "stage", "pass", ok=True,
               detail={"post_id": record["post_id"], "mode": record["mode"]})
        return {"ok": True, "post_id": record["post_id"], "mode": record["mode"],
                "live": bool(record.get("live"))}
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
             "model": p.model, "profile": p.profile,
             "available": bool(os.environ.get(p.env_key))}
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


def _defer_approver(run_id, rendered):
    """Dashboard human-gate approver: PARK the run at the Action hold instead of
    deciding now. The post is staged + AWAITING_HUMAN is logged; a human resolves
    it later via /api/approve or /api/reject. (Non-interactive on purpose; nothing
    posts until the human acts.)"""
    from gates.action import DeferApproval

    raise DeferApproval()


_defer_approver.interactive = False  # type: ignore[attr-defined]


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
    topic = (body.get("topic") or "").strip() or None   # dashboard topic box (overrides mission)
    # When the human-approval toggle is ON, the run blocks at the Action hold
    # (awaiting human) and only posts when the dashboard calls /api/approve.
    human_gate = bool(body.get("human_gate"))
    flags = {"reject_demo": scenario == "reject", "block_demo": scenario == "block",
             "faulty_grader": scenario == "faulty", "full_panel": scenario == "full"}
    run_id = uuid.uuid4().hex[:8]

    def worker():
        from harness import Harness, auto_approver  # lazy: heavy import

        # No global DRY_RUN mutation: a run NEVER posts live on its own. The
        # human-gated path parks at the hold (defer-approver); the auto path uses a
        # non-interactive approver, which select_client always downgrades to
        # dry-run. A real post can ONLY happen via an explicit, interactive
        # /api/approve with live=true.
        approver = _defer_approver if human_gate else auto_approver
        st = Store(DB_PATH)
        try:
            Harness(mission_path, st, real=real, preset=preset, approver=approver,
                    topic=topic, **flags).run(run_id=run_id)
        except Exception as e:
            try:
                st.log(run_id, "mission", "run", "error", ok=False, detail={"error": str(e)})
            except Exception:
                pass
        finally:
            st.close()

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id, "mission": mission_key, "preset": preset, "scenario": scenario}


@app.post("/api/runs/{run_id}/reject")
def api_reject(run_id: str, body: dict = Body(default={})):
    """Human reviewer HOLDS a post and types a free-text correction. Two effects,
    both DECLARED -- never a model update:

      (1) the correction is fed to the writer as the revision critique on the next
          attempt (a fresh run is launched with `human_feedback` wired into the
          existing revise loop);
      (2) ONLY if `save` is true (an explicit, confirmed click) the correction is
          appended as a soft guardrail to the mission's `*.learned.json` sidecar,
          which every future run loads. An unconfirmed correction never persists.
    """
    correction = (body.get("correction") or "").strip()
    if not correction:
        raise HTTPException(400, "a correction is required")
    save = bool(body.get("save"))

    st = store()
    try:
        events = st.events(run_id)
        mission_path = next((e["detail"]["path"] for e in events
                             if e["kind"] == "run" and e["name"] == "start"
                             and (e["detail"] or {}).get("path")), None)
        if not mission_path:
            raise HTTPException(404, f"no such run: {run_id}")
        saved = False
        if save:
            from harness import append_learned_guidance
            append_learned_guidance(mission_path, correction)
            saved = True
        st.log(run_id, "human", "revision", "human-reject", ok=False,
               detail={"correction": correction, "saved_as_guardrail": saved})
    finally:
        st.close()

    # Relaunch the mission with the correction wired into the writer's revise loop.
    preset = body.get("preset") or None
    scenario = body.get("scenario") or "normal"
    real = bool(body.get("real")) or bool(preset)
    topic = (body.get("topic") or "").strip() or None
    human_gate = bool(body.get("human_gate"))
    flags = {"reject_demo": scenario == "reject", "block_demo": scenario == "block",
             "faulty_grader": scenario == "faulty", "full_panel": scenario == "full"}
    new_id = uuid.uuid4().hex[:8]

    def worker():
        from harness import Harness, auto_approver

        approver = _defer_approver if human_gate else auto_approver
        st2 = Store(DB_PATH)
        try:
            Harness(mission_path, st2, real=real, preset=preset, approver=approver,
                    human_feedback=correction, topic=topic, **flags).run(run_id=new_id)
        except Exception as e:
            try:
                st2.log(new_id, "mission", "run", "error", ok=False, detail={"error": str(e)})
            except Exception:
                pass
        finally:
            st2.close()

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "run_id": new_id, "saved_as_guardrail": saved, "from_run": run_id}


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

    from harness import load_dotenv  # pick up .env (keys, presets availability)
    load_dotenv()
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  ◢ MISSION CONTROL dashboard  ->  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
