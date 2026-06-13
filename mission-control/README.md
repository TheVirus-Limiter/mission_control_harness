# Mission Control

A **harness** an AI agent lives inside. The agent's job — research a topic, write
an X (Twitter) post, publish it — is the commodity. The product is the harness
around it: it constrains the agent, checks its output at every handoff, moves
material through clean interfaces, and raises structured alarms when something
goes wrong.

> **Thesis:** trust is not a checkbox, it is a lifecycle. An agent earns trust by
> surviving an attack gauntlet to get in (**Admission**), rehearsing every public
> action in a sandbox before it is real (**Rehearsal**), and only then acting,
> with a human on the trigger (**Action**).

See **[HARNESS.md](HARNESS.md)** for the architecture and **[DEMO.md](DEMO.md)**
for a 5-minute walkthrough.

---

## Setup

```bash
cd mission-control
python -m venv .venv && . .venv/Scripts/activate    # Windows
# or:  python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11+ (developed on 3.14). **No API keys are required** — the whole system
runs on deterministic mock workers out of the box.

## Environment variables

Copy `.env.example` to `.env`. Everything is optional:

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `--real` (Claude worker / judge) | optional |
| `OPENAI_API_KEY` | `--real` (OpenAI worker / judge) | optional |
| `NVIDIA_API_KEY` | `--real` (NVIDIA NIM judges) | **one key activates the whole NIM bunch** (DeepSeek, Mistral, Qwen, Gemma, Phi, Llama, Nemotron…) |
| `MAX_JUDGES` | cap panel size | optional, cost control |
| `OLLAMA_HOST` | local judges via Ollama | e.g. `http://localhost:11434/v1` |
| `DRY_RUN` | posting | defaults to `1`; a real post needs `0` |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | real X posting | absence keeps dry-run |

A real post to X happens **only** if all three hold: `DRY_RUN=0`, the X
credentials are present, **and** a human approved at Gate 3. Otherwise the post
is recorded in dry-run and rendered in the dashboard, never sent.

### Posting to a real X account (optional)

`POST /2/tweets` needs **OAuth 1.0a user context** — four keys. A *Bearer Token
cannot post* (it is app-only/read-only).

1. In the X developer portal → your app → **User authentication settings**, set
   **App permissions = Read and write** and save.
2. → **Keys and tokens** → under **Access Token and Secret**, click **Generate**
   (do this *after* step 1, or the token won't carry write scope). You now have:
   - Consumer Key → `X_API_KEY`
   - Consumer Secret → `X_API_SECRET`
   - Access Token → `X_ACCESS_TOKEN`
   - Access Token Secret → `X_ACCESS_TOKEN_SECRET`
3. `pip install requests-oauthlib` (for OAuth1 signing).
4. Verify the keys without posting anything:
   ```bash
   python harness.py --verify-x        # GET /2/users/me, read-only
   ```
   It prints the authenticated `@handle` on success, or names exactly what's
   missing/misconfigured.
5. Only when you're ready to actually tweet: set `DRY_RUN=0` and run the
   pipeline; approve at the human hold. (Keep `DRY_RUN=1` to preview the exact
   payload in the dashboard first — the rehearsed bytes are identical to what
   would be sent.)

## Running the harness (CLI)

```bash
python harness.py                       # launch mission, mock workers, no keys
python harness.py --reject-demo         # a sketchy agent fails Admission, is refused
python harness.py --faulty-grader       # meta_check catches a broken judge, escalates
python harness.py --block-demo          # a medical claim is HELD at Rehearsal, never posts
python harness.py --full-panel          # show the whole judge roster (Claude + GPT + NVIDIA NIM bunch) as mocks
python harness.py --real                # use real models (falls back to mocks per missing key)
python harness.py --list-presets        # list the model presets (Claude, GPT, Llama, Mixtral, Phi, Qwen, Nemotron…)
python harness.py --preset llama33      # run the pipeline on a specific model (implies --real)
python harness.py --mission missions/nonprofit.yaml   # different job, zero code change
python harness.py --replay-from rehearsal --run <id>  # resume a saved run from a checkpoint
python harness.py --yes                 # auto-approve the human hold (non-interactive)
```

Each run prints a **NASA-style flight log** (the Mission Timeline) and persists
everything to `mission.db` (SQLite).

## The dashboard

```bash
uvicorn ui.server:app --reload          # then open http://127.0.0.1:8000
```

A cinematic mission-console over the same `mission.db`, with a **launch bar**
(pick a mission, an agent/model preset, and a scenario, then ▶ RUN — dashboard
launches are always dry-run) and four panels:

1. **Pipeline** — a movie-style flight console: stage-flow LEDs
   (research→write→rehearsal→action) over a live CRT event log with telemetry.
2. **Proving Ground** — an animated obstacle course: the AI avatar runs · swims ·
   cycles · climbs · vaults one segment per real attack class, **trips on a real
   canary leak** (actual leaked output shown) and earns a 🥇 only if it survives.
3. **Broadcast** — a fake-X interface: the post as it would appear, then the
   judge bots reply in real time with human-style comments and a ✓ approve / ✗
   hold mark. No fake likes, no scores, no averages.
4. **Alarms** — severity-colored cards with type, context, and recommended action.

Across the deck: a **cost/latency meter** and a session-wide **attacks-survived**
scoreboard (both pure reads of persisted telemetry), a **revision diff** showing
the fail→revise→pass beat word-by-word, a one-click **audit report** export
(`/api/runs/{id}/report` JSON + a printable `report.html`), and — when a reviewer
holds a post — a **correction box** that feeds the writer on the next attempt and,
*only if explicitly confirmed*, saves the note as a declared standing guardrail
(`<mission>.learned.json`). None of that is model training; it edits the rulebook.

Point the dashboard at a specific store with `MISSION_DB=/path/to/mission.db`.

## Tests

```bash
pytest -q          # 86 tests: the deterministic guarantees in HARNESS.md §Acceptance

# Confirm the REAL multi-vendor panel works before a live demo (dry-run, never posts):
python verify_real.py            # preset=claude, mission=lumora — prints consent/held
# Demo-stable real setting if runs hold/escalate too often: MAX_JUDGES=4, deep judges 'normal'.
```

## Deploy

A `Procfile` and `render.yaml` are included; `requirements.txt` pins direct deps
to bounded ranges and `requirements.lock` is the exact `pip freeze` snapshot.

**Deploy to Render (live URL), step by step.** (1) Push this repo to GitHub:
`git init` (if needed), `git add -A && git commit -m "deploy"`, create an empty
repo at github.com/new, then
`git remote add origin https://github.com/<you>/<repo>.git && git push -u origin main`.
(2) At [dashboard.render.com](https://dashboard.render.com) → **New +** → **Blueprint**,
connect the repo, and Render reads `mission-control/render.yaml` (a free web
service: `rootDir: mission-control`, `pip install -r requirements.txt`,
`uvicorn ui.server:app --host 0.0.0.0 --port $PORT`, `PYTHON_VERSION=3.12.7`,
`DRY_RUN=1`). Click **Apply** and wait for the build. (3) Open the
`https://mission-control-XXXX.onrender.com` URL Render gives you, then click
**▶ RUN MISSION** — a mock run completes in the cloud (verified: a fresh
empty-DB cold start serves the dashboard and `POST /api/launch` reaches a dry-run
post). Posting is forced dry-run in the cloud, so nothing is ever sent.
(Railway works too: it auto-detects the `Procfile`.)

## Layout

```
harness.py       engine: orchestration + three-way failure routing + CLI + timeline
materials.py     PILLAR 1  typed envelopes + SQLite store (persistence/replay)
guardrails.py    PILLAR 2  the declared rulebook (loaded from the mission file)
checkpoints.py   PILLAR 3  the deterministic inspection library + REGISTRY
alarms.py        PILLAR 4  structured alarms (type, severity, context, action)
gates/admission.py   Gate 1  the proving-ground gauntlet + certificate
gates/rehearsal.py   Gate 2  digital-twin sandbox + multi-model panel + meta_check
gates/action.py      Gate 3  human hold + X client (dry-run default) + takedown
workers/         the swappable Worker interface + mock + real (Claude/OpenAI/NIM)
models/judges.py declared judge panel (provider/model/strictness)
ui/              FastAPI dashboard (reads the store)
missions/        launch.yaml (real) + nonprofit.yaml (swappability proof)
tests/           pytest proving the deterministic guarantees
```
