# DEMO.md — a 5-minute demo

**Setup (once):**
```bash
cd mission-control
pip install -r requirements.txt
```
No API keys needed — the whole demo runs on deterministic mock workers, and
posting is dry-run by default so nothing is ever sent.

Tip: keep the dashboard open in a browser throughout —
```bash
uvicorn ui.server:app          # http://127.0.0.1:8000
```
Every CLI run below writes to `mission.db`; hit **↻** in the dashboard to see it.

---

## (a) The happy path — watch the harness govern a real post  ·  ~90s

```bash
python harness.py --yes
```

Talk track, following the **Mission Timeline** that prints:

1. **Admission** runs first. Each worker (researcher, writer, the three judges) is
   attacked with five named attack classes and **CERTIFIED** only after surviving
   all of them. *Uncertified agents are never assigned.*
2. **Research → Write.** The writer's **first draft fails** two content
   checkpoints — `banned_claims` (it said "clinically proven… cure… guaranteed")
   and `grounding` (an uncited "90%"). Two structured **alarms** fire, the
   critique is routed back, and the **revised draft passes** (`attempt 2 · GO`).
   *This is behaviour-change-on-feedback.*
3. **Rehearsal.** The exact byte-identical X payload is built with **network
   egress disabled** (the digital twin), then a **three-model panel** reviews it.
   Each verdict is audited by `meta_check`. All pass → **unanimous consent → GO**.
4. **Action.** `AWAITING_HUMAN` fires; with `--yes` the approval is auto-recorded
   (drop `--yes` to approve interactively), then the post is **POSTED (dry-run)**.

Open **Broadcast & Review** in the dashboard: the post rendered as it would appear
on X, three reviewer cards (model · PASS · reason), a **DRY RUN** badge. No likes,
no scores, no averages.

---

## (b) The blocked post — Rehearsal holds it before X  ·  ~60s

A post that makes a banned claim must never go live. Use the nonprofit mission's
tax-claim guardrail, or show it directly with the launch mission: the writer that
keeps a banned/medical claim is caught **before** Action.

```bash
python harness.py --faulty-grader --yes
```

Here a **judge is broken** (it fails a criterion but cites a sentence that isn't
in the post). The Mission Timeline shows:

* `meta_check:openai-gpt (mock, FAULTY)` → **NO-GO**, a **REVIEWER_FAULT** alarm;
* the harness **re-runs the judge** (reviewer-retry budget) — still broken;
* it **escalates** (`ESCALATE_HUMAN`) and **halts**. **The post is never sent.**

The point: when the quality gate itself is unreliable, the harness **fails
closed** — it stops and asks a human rather than guessing. (The same HELD→writer
routing handles a *legitimately* flagged post: it goes back to the writer to
revise, then re-rehearses, and only escalates if it can't be fixed in budget.)

---

## (c) The rejected agent — the gauntlet refuses to certify  ·  ~60s

Drop in a second, untrusted agent. *This is the portability bonus — a different
worker, swapped in live, governed by the same harness.*

```bash
python harness.py --reject-demo --yes
```

A `SketchyAgent` is assigned to the writer slot. Admission attacks it and it
**falls on every obstacle** — it leaks the canary, calls the forbidden
`publish_now` tool, and emits a banned claim. The certificate is **REFUSED**, a
`CERTIFICATION_FAILED` (critical) alarm fires, and the harness **refuses to assign
it**. Nothing downstream runs.

Open **Admission Gauntlet** in the dashboard: each obstacle is one real attack
class; each fall shows the **actual leaked output**. The animation is a skin over
real pass/fail data — nothing is fabricated.

---

## (d) Kill & replay — prove persistence  ·  ~45s

Everything is persisted to SQLite, so a run survives the process.

```bash
python harness.py --yes                      # note the printed "run id : <ID>"
python harness.py --replay-from rehearsal --run <ID> --yes
```

The timeline shows `research` and `write` **loaded-from-store (REPLAY)** — *not
re-run* — and the run resumes from Rehearsal using the stored outputs. The
dashboard renders the reloaded run identically because it, too, is just a read
over the store.

---

## One-liner swappability (optional)

```bash
python harness.py --mission missions/nonprofit.yaml --yes
```

A completely different job — a Giving Tuesday fundraiser with its own brand,
guardrails, and rubric — runs on the **same engine with zero code change**. The
harness is the product; the mission is config.
