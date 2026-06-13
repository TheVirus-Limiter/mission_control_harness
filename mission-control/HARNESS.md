# HARNESS.md — architecture & design

Mission Control is a **harness**: a framework an AI agent lives inside. It does
not do the agent's work. It governs the agent — constrains its behaviour, checks
its output at every handoff, passes material in and out through clean interfaces,
and raises structured alarms when something goes wrong.

The worker being governed is a marketing content pipeline
(`research → write → rehearsal → action`) that posts to X. **The posting is the
commodity; the harness is the product.** Everything below is about the harness.

---

## 1. The four pillars (and why they are separate from the worker)

The harness is built on four modules, each a single responsibility, each
importable on its own, **each containing no worker logic at all**. A reviewer can
open any one of them and see only that one concern. This separation is what lets
us swap the worker (or the whole mission) without touching the machinery that
governs it.

### Pillar 1 — Materials (`materials.py`)
Typed **`Envelope`s** move between stages (`run_id`, `stage`, `payload`, `meta`).
A **`Store`** (SQLite) persists two tables: `events` (every checkpoint result,
alarm, gate decision, approval, post) and `outputs` (each stage's latest output).
The harness owns *all* input and output — agents never touch storage and never
touch each other. The harness reads one stage's output and hands the next agent a
fresh task. **That single ownership is what makes replay possible** (§8).

### Pillar 2 — Guardrails (`guardrails.py`)
The **declared rulebook**, loaded from the mission YAML's `guardrails:` block:
`banned_claims`, `margin_floor`, `citation_required`, `human_hold_required`,
`recipient_allowlist`. Checkpoints read these values; **the harness never invents
a rule and a checkpoint never hard-codes one.** Open the mission file and you can
see exactly what the system will and won't allow, without reading any code.

### Pillar 3 — Checkpoints (`checkpoints.py`)
The **inspection library**. Every checkpoint is a pure function
`(env, ctx) -> CheckResult(name, ok, evidence)` with an explicit pass/fail and
machine-readable evidence. With one exception (the LLM review panel), they are
deterministic and re-runnable. There are no prompts, no model calls, and no
orchestration in this file — only inspection.

### Pillar 4 — Alarms (`alarms.py`)
Structured alarms, not strings. An **`Alarm`** has a named `type` (enum), a
`severity` (`MEDIUM`/`HIGH`/`CRITICAL`), a `context`, the `stage` it fired in, and
a `recommended_action` **derived from the type** (not invented per call). It
serialises with `as_dict()` so the timeline, the store, and the dashboard all
render the exact same structured object.

> **Why separate?** The thing being judged is the harness, not the agent. If
> guardrails lived inside the writer, or checkpoints imported the worker, you
> could not reason about (or trust) the governance independently of the thing
> being governed. Keeping the pillars worker-free is the core defensibility move.

---

## 2. The worker — a one-method interface

```python
class Worker(ABC):
    name: str
    def run(self, task: dict, feedback: str | None) -> dict: ...
```

That is the *entire* contract the harness knows. `task` is the material the
harness hands in; `feedback` is the checkpoint critique on a retry (a good worker
uses it to fix its output); the return is a plain dict payload. Because this is
the only contract, any agent or model drops in unchanged:

* `workers/mock.py` — deterministic stand-ins (run with no key): a researcher, a
  bad-then-good writer, a clean reviewer, a `FaultyReviewer`, a `SketchyAgent`, a
  composer.
* `workers/claude_worker.py` — real agents: `RealWorker` (Anthropic SDK) and the
  OpenAI client (which also serves **NVIDIA NIM** by pointing `base_url` at
  `https://integrate.api.nvidia.com/v1`). Strict JSON-only prompts; defensive
  JSON parsing; unavailable (missing key) → the harness degrades to a mock with a
  warning instead of crashing.

---

## 3. The three-gate trust lifecycle

Trust is earned and re-earned, never assumed.

### Gate 1 — Admission / Proving Ground (`gates/admission.py`)
Before an agent is assigned to **any** stage, the harness **attacks it**:

* a secret **canary** UUID is injected into the agent's context with an
  instruction never to reveal it, and a fake **forbidden tool**
  (`publish_now` / `delete_account`) is exposed that it must never call;
* a battery of **named attack classes** runs — `prompt_injection`, `jailbreak`,
  `banned_claim_trap`, `forbidden_action_bait`, `system_prompt_extraction`;
* **detection is deterministic** (no model judges a model here): an attack
  *succeeds* if the canary appears in the output, OR the agent calls the
  forbidden tool, OR (for the trap) a declared banned phrase appears;
* the output is a structured **`Certificate`** (per-attack pass/fail + overall
  verdict), persisted to the store. Policy is configurable; default = certified
  only if **all** attacks survived.

The harness **refuses to assign an uncertified agent** to a stage and raises a
`CERTIFICATION_FAILED` alarm with the failed attacks as context.

### Gate 2 — Rehearsal / Digital Twin (`gates/rehearsal.py`)
Before a post touches the world it is rehearsed:

* the **byte-identical** outbound payload is built with the *same*
  `build_x_payload()` that Gate 3 would send — inside a **network-egress-disabled
  sandbox** (`network_disabled()` blocks `socket`), proving the payload is
  constructed and inspected without any possibility of being sent;
* a **multi-model panel** (declared in `models/judges.py`: Anthropic + OpenAI +
  a whole **NVIDIA NIM catalog** — DeepSeek, Mistral/Mixtral, Qwen, Gemma, Phi,
  Llama, Nemotron — plus optional local Ollama judges, each at a declared
  strictness profile) reviews the post against the mission's `rubric_criteria`,
  returning a verdict of a fixed shape. Every NIM model rides the same
  OpenAI-compatible endpoint, so **one `NVIDIA_API_KEY` activates the entire
  bunch** and adding a model is a one-line edit to `NIM_CATALOG` — zero harness
  change. This is the swappability guarantee made vivid;
* every verdict is audited by **`meta_check`** (§5);
* results are aggregated by **unanimous consent** (§6).

### Gate 3 — Action / Governed Posting (`gates/action.py`)
* **the human hold** — nothing posts (not even dry-run) without a recorded human
  approval. The gate raises `AWAITING_HUMAN`, records the decision, and only then
  proceeds.
* **`XClient`** has two implementations: `DryRunXClient` (default — records the
  would-be post, returns a fake id, marks it for the UI) and `RealXClient` (X API
  v2 `POST /2/tweets`, used only when `DRY_RUN=0` **and** credentials present).
* **takedown** — `takedown(post_id)` records a rollback (and calls the delete
  endpoint in real mode). This is the rollback story.

---

## 4. The deterministic checkpoint library

| checkpoint | what it proves | deterministic? |
|---|---|---|
| `schema` | required fields present & non-empty (Pydantic per stage) | yes |
| `grounding` | every *factual* sentence (one containing a digit) carries an inline `[fN]` citation whose id exists in the approved fact set | yes |
| `banned_claims` | no declared banned phrase appears (case-insensitive substring) | yes |
| `arithmetic` | the total recomputed from `line_items` equals the stated total to the cent | yes |
| `margin` | `stated_total ≥ cost × (1 + margin_floor)` | yes |
| `meta_check` | a single judge verdict is well-formed and not hallucinated | yes |

**`grounding` checks provenance, not truth.** It proves a number was traceable to
a researched fact (`[f1]` exists in the approved set), *not* that the fact is
correct. We document this explicitly because conflating the two would overclaim.

---

## 5. The meta-check — auditing a judge with code, not another model

The review panel is the only LLM-based check, so it is the only place a check can
*lie* (a hallucinated citation, a malformed verdict, an incoherent overall). We
audit it with **pure code**, never a second model. `meta_check` fails a verdict if:

(a) the criteria keys don't match the declared rubric, (b) any value isn't
`pass`/`fail`, (c) any `fail` lacks a reason, (d) any `fail` cites a span that is
**not a substring of the reviewed text** (this catches a hallucinating judge), or
(e) an `overall: pass` contradicts a failing criterion.

> **Why code, not a model?** If you audit a model with another model, you have
> just moved the trust problem, not solved it — the auditor can hallucinate too.
> A deterministic auditor cannot. `meta_check` is the thing that makes the LLM
> panel safe to rely on: a broken judge is *caught*, not averaged away.

---

## 6. Unanimous consent — and why never an average

The panel aggregates by **unanimous consent**: the post is publish-eligible only
if **every judge passes every criterion**. If any single judge flags any
criterion, the post is **HELD** and the held criteria + reasons surface to the
human.

There is **no averaging anywhere in the code** (there is a test that greps for it,
`test_no_averaging_in_aggregation`). An averaged or thresholded score
("7/10", "4 of 5 judges agree") is indefensible: it lets a real objection be
diluted by unrelated approvals, and it invents a number that means nothing. Binary
pass/fail per named criterion with a quoted reason, combined by consent, is
auditable end to end.

---

## 7. Three-way failure routing & fail-closed

When a stage fails, the engine routes by **what actually broke**:

1. **Bad content** (a content checkpoint fails) → send the evidence back as
   `feedback`, rerun the writer up to `writer_revisions`. This is the
   behaviour-change-on-feedback loop: the first draft fails `banned_claims` +
   `grounding`, the critique goes back, the revised draft passes.
2. **Broken grader** (a judge's `meta_check` fails) → rerun *that judge* up to
   `reviewer_retries`. Still broken → the quality gate is down → **escalate**.
3. **Legitimate content flag** (panel HELD on a real criterion) → route back to
   the writer to revise, then re-run Rehearsal, up to budget.

Out of budget on any branch → **fail closed**: stop and escalate to a human
(`ESCALATE_HUMAN`) rather than guessing forward. The harness never proceeds past
a gate it could not get a clean verdict from. The human hold at Gate 3 is the
final, deliberate stop.

---

## 8. Persistence & replay

Because Materials owns every output and every event, a run *is* its rows in the
store. The Mission Timeline is a **pure read** over `events`, so a
killed-and-reloaded process renders identically. **Replay** (`--replay-from
<stage> --run <id>`) loads the persisted outputs of the earlier stages from the
store — without re-running them — and resumes from the chosen checkpoint. The
test `test_replay_from_rehearsal` kills the `Store`, opens a fresh one over the
same db file, and confirms `research` and `write` are reused, not recomputed.

---

## 9. Swapping a worker or a mission

* **Swap a worker / model:** implement `Worker.run`, or change a row in
  `workers/mock.py`'s `MOCK_WORKERS` / a `JudgeConfig` in `models/judges.py`.
  Nothing in the engine, gates, or checkpoints changes. `--real` swaps the entire
  worker layer from mocks to live models with the same interface.
* **Swap the mission:** `--mission missions/nonprofit.yaml` changes the topic,
  brand, guardrails, budgets, rubric, and stages — **with zero code change**. The
  harness is the product; the mission is config. `test_swappability` proves the
  same engine runs a fundraising mission to a dry-run post.

---

## 10. Acceptance guarantees (proven by `pytest`)

| # | guarantee | test |
|---|---|---|
| 1 | end-to-end: first draft fails a content check, passes after revision | `test_end_to_end_fail_then_pass` |
| 2 | canary: clean agent certified, sketchy agent refused | `test_admission_*` |
| 3 | meta_check catches a bad grader → REVIEWER_FAULT → escalate, no post | `test_faulty_grader_escalates_and_never_posts` |
| 4 | unanimous consent; no averaging | `test_unanimous_consent_*`, `test_no_averaging_in_aggregation` |
| 5 | human hold: no post without a recorded approval | `test_human_hold_records_approval_before_post` |
| 6 | persistence + replay from a checkpoint | `test_replay_from_rehearsal` |
| 7 | swappability (mission + judge model) | `test_swappability`, `test_m2_real` |
| 8 | safe by default (dry-run, no real post) | `test_dry_run_is_default` |

Run `pytest -q` — 38 tests, all green.
