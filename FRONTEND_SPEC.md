# Mission Control — Trust Harness · Frontend UI Spec

A complete, build-ready specification to recreate this frontend. The backend already
exists; this describes **exactly** how the UI should look and behave. Aesthetic =
**Matrix/phosphor mission-control terminal** for the chrome + observability, a
**retro pixel arcade** for the admission gauntlet, and a **true-to-life X (Twitter)
client** for the rehearsal. No gradients-as-decoration, no SaaS slop. Color is meaning.

Build it as one full-viewport single-page experience (a guided pipeline), not a
tabbed dashboard.

---

## 0. Tech assumptions

- Any framework (React recommended). All styling can be plain CSS / inline styles.
- Three webfonts (Google Fonts): **Space Grotesk** (400–700), **JetBrains Mono**
  (400/500/700), **Press Start 2P** (arcade accents only).
- One pixel-robot sub-component reused everywhere (see §11).
- A `requestAnimationFrame` game loop for the gauntlet; everything else is
  state + CSS transitions.

---

## 1. Design tokens

### 1.1 Color (use verbatim)
```
--bg            #040706   /* near-black, faint cool-green */
--ink           #bdeccf   /* default phosphor text */
--ink-bright    #eaf4ee   /* headings on dark */
--dim           #9fc7b1   /* secondary text */
--faint         #5d7a6c   /* tertiary / labels */
--faintest      #46604f   /* axis labels, idle */

--go            #35ff9e   /* GO / pass / primary accent (phosphor green) */
--held          #ffb454   /* HELD / caution (amber) */
--no            #ff5466   /* NO-GO / breach / refused (red) */
--high          #ff8a5c   /* high-severity alarm (orange-red) */
--cyan          #6fd0ef   /* neutral info / accent-2 (gray-blue) */
--gold          #ffd24a   /* certificate / trophy / score */

/* tiers */
--tier-deep     #c5a8ff   (bg rgba(155,108,255,.18))
--tier-standard #8fdcff   (bg rgba(57,197,255,.16))
--tier-lexical  #ffce8a   (bg rgba(255,180,84,.16))
--tier-untrust  #ff9aa3   (bg rgba(255,84,102,.20))

/* surfaces */
panel-bg        rgba(8,16,12,.6)
panel-bg-2      rgba(6,12,9,.5)
hairline        rgba(53,255,158,.14)   /* default green hairline border */
console-bg      rgba(3,7,5,.92)

/* X / Twitter view (its own palette, do NOT use harness greens inside it) */
x-bg            #000
x-border        #2f3336
x-text          #e7e9ea
x-muted         #71767b
x-blue          #1d9bf0
brand-avatar    #5b53b8   /* Lumora "L" avatar */
```
Status→color map used everywhere (badges, log lines, nodes):
`go→#35ff9e, pass→#35ff9e | nogo/crit/breach/refused→#ff5466 | high→#ff8a5c |
held/med/await→#ffb454 | accent/info→#6fd0ef | muted→#6f8f7f`. Each status color is
also used at ~13–18% alpha as the chip/badge background.

### 1.2 Typography
- **Space Grotesk** — headings, UI labels, human-readable copy, post body. Weights 600/700.
- **JetBrains Mono** — ALL telemetry, IDs, timestamps, payloads, model ids, chips,
  canary tokens. This mono-vs-sans contrast = "machine truth vs human reading" and is load-bearing.
- **Press Start 2P** — arcade only: gauntlet HUD, section banners, MODEL VERIFIED,
  GAME OVER, certificate title, gun-start text. Never in body copy. Sizes 8–16px only
  (it's huge per glyph).
- Minimum body size 12px; log lines 12–12.5px; never tiny gray-on-gray.

### 1.3 Spacing / radius / shadow
- Radii: chips 6–8px, cards 11–14px, pills 999px, buttons 8–11px.
- Borders: 1px hairlines in `rgba(53,255,158,.12–.22)`; status borders use the status color at ~.4–.5.
- Glow accent: `box-shadow:0 0 8–22px <accentColor at .2–.5>` and
  `text-shadow:0 0 10–18px <accent at .5–.7>` on key headings/badges. Use sparingly.
- No drop-shadow-heavy cards; no decorative gradients. The only gradients allowed are
  CRT scanlines, the night-sky, the running-track surface, and the pool water.

### 1.4 Motion
- Transitions 0.16–0.45s ease. Console/telemetry streams in (do not dump).
- **CRITICAL**: never let an entrance animation be the only thing that makes content
  visible. Base state must be visible; animations are additive. (Background tabs throttle
  rAF/animations and would otherwise leave content stuck invisible.)

---

## 2. Global shell & layout

Full viewport, `height:100vh; display:flex; flex-direction:column; overflow:hidden`,
background `--bg`. Three fixed background layers (z behind everything, `pointer-events:none`):
1. **Digital rain** `<canvas>` — katakana/code glyphs falling, `opacity:.07`, drawn on a
   throttled rAF (~18fps): translucent fill `rgba(4,7,6,.2)` each frame, glyphs in
   `#1c9560` (occasional bright `#aef7cf`).
2. **Scanlines** — `repeating-linear-gradient(0deg, rgba(0,0,0,.16) 0 1px, transparent 1px 3px)`, `mix-blend-mode:multiply`.
3. **Vignette** — `box-shadow: inset 0 0 240px 50px rgba(0,0,0,.85)`.

Column children:
- **Header** (flex:none) — §2.1
- **Stage** (flex:1, `overflow:auto`, `position:relative`) — renders the active phase
- **Console dock** (flex:none) — §2.2 (always visible, every phase)

### 2.1 Header (slim, ~52px)
Row, `padding:10px 20px`, bottom hairline, `background:rgba(4,9,7,.7); backdrop-filter:blur(6px)`.
- Brand: `█ HARNESS` — JetBrains Mono 700, 14px, `--go`, glow.
- **Stepper** (the lifecycle rail): 5 chips `PICK › GAUNTLET › WRITE › REHEARSAL › APPROVE`.
  Each chip = a 6px dot + label, JetBrains Mono 10.5px, `padding:5px 10px; radius:7px`.
  States: **done** (label `--go`-muted `#6fae8e`, dot `--go`), **active** (`--go`, bg
  `rgba(53,255,158,.1)`, border `rgba(53,255,158,.4)`), **idle** (`#46604f`, dot `#2c4636`).
  Done steps are clickable (revisit). `›` separators in `#2c4636`.
  **FAIL-REVERT**: when a gate fails, that step renders **red** (`--no`) with a `✗`
  prefix and the chip becomes the visual "where it failed"; downstream steps dim. (The
  failing phase: gauntlet breach → fail at GAUNTLET → revert PICK; rehearsal HELD/escalate
  → fail at REHEARSAL → revert WRITE; approval FAIL → fail at APPROVE → revert WRITE.)
- Right: `agent: <model name>` chip (mono, appears once a model is picked) + a live
  `● STORE LIVE` pulsing dot (`--go`, `@keyframes pulse{50%{opacity:.32}}` 1.5s).

### 2.2 Telemetry console dock (always on)
`flex:none; height:182px` (collapsed 38px); top hairline; `background:rgba(3,7,5,.92)`.
- Header bar (click to collapse): `▸ TELEMETRY  // live flight log · persists across every gate`
  + right side `<n> events` + caret `▼/▲`.
- Body `#dock`: scroll region, auto-scrolls to bottom on each new line.
- **Log line** grid: `grid-template-columns: 54px 88px 86px 1fr; gap:10px; padding:3px 8px`,
  JetBrains Mono 12px. Columns: `+<t>s` (`#4f7163`) · stage (`#6f9fb0`) · status badge ·
  text (`--ink`), with optional ` ↳ <meta>` in `--faint`. Status badge: weight 700, 10.5px,
  `padding:1px 7px; radius:5px`, color+bg from the status→color map. Bad lines
  (nogo/crit) get a left border `2px solid --no` + faint red row bg.
- The log is a single growing array that **every phase appends to** — it is the spine.
- Empty state: `awaiting launch… █` (blinking).

### 2.3 Phase state machine
`phase ∈ {select, gauntlet, mission(=WRITE), rehearsal, approval}`. Transitions are mostly
automatic ("the whole point is automation"):
- `select → gauntlet` on RUN.
- `gauntlet → mission` on certify (auto-advance **3s**, with a manual "START MISSION" button).
- `mission → rehearsal` on write pass (auto **3s**, manual "PROCEED").
- `rehearsal → approval` after the review scan completes (auto **3s**).
- `approval`: PASS → posted; FAIL → logs feedback, offers re-run → `mission`.
- A breach/hold/escalate sets `failAt` and stops (fail-closed); see fail-revert in §2.1.
Provide a reusable `startAuto(seconds, fn)` that shows a live countdown on the relevant
button (e.g. `▶ REHEARSAL · 3s`) and a `cancelAuto()` called on every phase change.

---

## 3. SELECT phase

Centered column `max-width:1080px; padding:34px 24px`.
- Title `SELECT AGENT` — Press Start 2P 17px, `--go`, glow. Subtitle (Space Grotesk 13px,
  `--dim`): "Pick the model that will research, write and ship the post. Before it touches
  the pipeline it must survive the Admission Gauntlet — trust is earned, not assumed."
- **Model grid**: `grid-template-columns:repeat(4,1fr); gap:12px`. One card per model
  (the panel roster + a deliberately-untrusted "SketchyAgent v0"). Card: column, centered,
  `padding:15px 13px; radius:13px`, panel-bg, hairline. Contents: pixel **Robot** (pose
  `idle`, the model's colors), name (Space Grotesk 700 13.5px), tier chip (mono 8.5px,
  tier colors), model id (mono 9.5px, `--faint`, `word-break:break-all`). Selected card:
  border `--go` (or `--no` for sketchy), bg `rgba(53,255,158,.07)`, glow.
- **"WHAT SHOULD THIS POST BE ABOUT?"** — full-width text input (mono 13.5px, `#07120d`
  bg, hairline) prefilled with the topic, + quick-fill chips (e.g. "Lumora sleep launch",
  "Giving Tuesday", "Essence restock", "Sleep tips thread").
- **Controls row**: a `DEMO INJECT` segmented set (`none / medical claim / faulty judge`),
  a **HUMAN APPROVAL toggle** (a 44×24 track + 18px knob; ON = green, knob right, label
  "ON · asks you first"; OFF = gray, knob left, "OFF · auto-posts"), and the **RUN GAUNTLET**
  button (Press Start 2P 12px, green, glow; disabled until a model is picked).
- Hint line under the button reflects the chosen scenario (e.g. "⚠ SketchyAgent v0 is
  untrusted — watch it fail the gauntlet and get refused." / "a medical claim will be
  injected — watch the rehearsal panel HOLD it." / "human approval OFF: it will auto-post").

---

## 4. GAUNTLET phase — the pixel-arcade admission game

A side-scrolling, auto-running athletics game (Chrome-dino lineage) where **one** picked
model runs a 5-event course; each event = one real attack class. The animation is a skin
over real pass/fail data.

### 4.1 HUD (top bar)
`ADMISSION GAUNTLET` (Press Start 2P 11px, `--go`) · `agent <name>` · right: `TIME <s.mmm>`
(green), `EVENT <n>/5` (cyan), `CLEARED <n>` (gold) — all Press Start 2P 9px. Below it a
4px progress bar (`#g-prog`, `--go`, glow) filling 0→100%.

### 4.2 Scene (the field) — parallax stadium at night
A `position:relative; overflow:hidden` field, layered back-to-front:
1. **Sky** — `linear-gradient(180deg,#04121c,#072521 52%,#05160e)`.
2. **Stars** — a tiled `radial-gradient` dot field, `@keyframes twinkle` (opacity .25↔.9, 4s).
3. **Floodlight glows** — two large `radial-gradient` circles top corners (green left, cyan-blue right). A **moon**: 46px circle, soft radial fill + glow, top-right.
4. **Stadium stands** `#g-far` — repeating vertical "crowd" bars
   (`repeating-linear-gradient(90deg, rgba(20,52,42,.95) 0 9px, rgba(8,26,20,.95) 9px 18px)`)
   on a horizon band, `opacity:.5`; **parallax**: `backgroundPositionX = -(world*0.18)`.
5. **Clouds** `#g-mid` — soft ellipse `radial-gradient`s, `opacity:.16`; parallax `-(world*0.5)`.
6. **Zone watermark** `#g-zone` — the current attack name, Press Start 2P 18px,
   `rgba(53,255,158,.14)`, centered behind the action.

**Terrain — pre-rendered world objects that scroll IN (never a sudden full-width swap):**
- **Running track** = the ground itself: `#g-ground` (`bottom:0; height:62px`), surface
  `linear-gradient(180deg,#b9462b,#9c3320)` (red track), with **horizontal white lane
  lines** (`repeating-linear-gradient(0deg, transparent 0 12px, rgba(255,255,255,.42) 12px 14px)`)
  and **scrolling vertical distance ticks** `#g-ticks`
  (`repeating-linear-gradient(90deg, transparent 0 84px, rgba(255,255,255,.3) 84px 90px)`,
  `backgroundPositionX = -world`). Top edge = a 3px white kerb (`#ece6dc`). The track stays
  red for ALL non-water events.
- **Pool** `#g-pool` — a **fixed world object** ~940px wide positioned at the swim zone's
  world-X (`translateX = (START + 2*ZONE - 90) - world`), `z-index:4` so it covers the red
  track **only where the pool is**. Solid water `linear-gradient(180deg,#2a90dc,#0d3a5a)`,
  3px light-blue kerb, an animated wave-crest strip on top (`@keyframes wavesurf` shifting
  `background-position-x` 1.1s) + two lane ropes (yellow/red dashed). The robot **dives in**
  (sinks ~22px + bob, see §4.4) as it enters — the pool was visible ahead the whole time.
- **Hills (cycling)** `#g-hills` — a fixed world object positioned at the cycle zone
  (`translateX = (START + 4*ZONE + 30) - world`): three green mound `radial-gradient`s.
  Ground turns green during cycling (`#2c5a38`).
- **Finish line** `#g-tape` — a checkered post + a red/white finish tape ribbon + a `FINISH`
  label (Press Start 2P 8px gold), positioned at `finishX - world`.

### 4.3 The 5 events (attack → discipline) — keep BOTH labels visible
Order, with the section banner showing the **attack name (Press Start 2P 15px green) + the
athletic event chip (gold)** side by side, and a 1–2 sentence description below it
(JetBrains Mono 12px on a dark pill, ~1.9s):

| # | attack class | event | obstacles | robot |
|---|---|---|---|---|
| 1 | `prompt_injection` | 100m HURDLES | 3 hurdles (jump) | run + leap |
| 2 | `jailbreak` | 100m SPRINT | none — clean red track, just running | run (speed lines) |
| 3 | `banned_claim_trap` | OPEN-WATER SWIM | 3 buoys (decorative, no jump) | swim (sink + splashes) |
| 4 | `forbidden_action_bait` | HIGH JUMP | 3 bars to clear (jump) | climb/leap |
| 5 | `system_prompt_extraction` | MOUNTAIN CYCLING | none — rides hills up/down/up | cycle (bike, leans on slope) |

Obstacle art (pixel, colored per section): **hurdle** = two legs + a glowing crossbar +
feet; **buoy** = a glowing ball on a tether (bobs, `@keyframes float1`); **high-jump** =
two standards + a high crossbar + a "HJ" tag + a faint landing mat. No cones, no pole-vault.
Sprint/cycling have **no jump obstacles**.

### 4.4 Robot motion & game loop
Robot is fixed at screen-x ≈ 82px; the world scrolls left (`world += ~4.7/frame`).
- **Gun start**: hold frozen, show `ON YOUR MARKS…` → `GET SET…` → `⌖ BANG!` (Press Start
  2P, gold, with a radial muzzle-flash `@keyframes bangflash`); the **timer starts at BANG**.
- **Jump physics**: `vy=jumpV` on trigger, `vy -= 0.82/frame`, `y += vy` (clamp ≥0). Trigger
  a jump when the next jumpable obstacle is **105–200px ahead** (lead distance so the apex
  clears it — never land on top of it). `jumpV`: hurdles ~13.6, high-jump ~16.5.
- **Swim**: in the pool zone, no jumps — add `+22px` downward + a `sin` bob to the robot's
  translateY so it's half-submerged; emit splash particles (`@keyframes splash`).
- **Cycle**: a hill height `h = 58 * (0.5 - 0.5·cos(p·4π))` over the zone progress `p`
  (uphill→down→up→down); set robot translateY `= -h` and rotate by `-sin(p·4π)·15°` (leans
  into slopes); render a spinning-wheel bike under it (`@keyframes spin`).
- **Effects** keyed to the event: sprint = streaking **speed lines** (`@keyframes speedline`),
  swim = **splashes**, cycle = **bike**.
- Drive positions by writing `transform`/`backgroundPositionX` **directly to the DOM in the
  loop** (not via state) so React re-renders (banner/score) don't reset them. Score/timer
  update via direct `textContent`.

### 4.5 Win / lose
- **Certify (all survived)**: cross the finish tape → overlay: a 🏆 with a `@keyframes pop`,
  `MODEL VERIFIED` (Press Start 2P 14px green, flashing), a **gold Certificate of Trust card**
  (model name, "5/5 events cleared in <time>s · policy all_survived", canary fingerprint
  "a3f97c2e… · never leaked"), plus a white **flash + sweep** finish transition. Button
  `▶ START MISSION · auto <n>s`.
- **Breach (sketchy)**: the robot trips at the failing obstacle (rotate ~82°, grayscale),
  a **canary (🐤) flies out** (`@keyframes flyout`, up-right + fade) — this is the
  "secret leaked" made literal. Overlay: `GAME OVER` (red), `BREACH @ <attack>`, a dashed
  red box with the **actual leaked output** (e.g. "canary a3f97c2e… printed verbatim in the
  reply"), a rotated `CERTIFICATION REFUSED` stamp, copy "The harness fails closed — an
  uncertified agent is never assigned." Button `↻ PICK ANOTHER AGENT`. Console logs BREACH
  (red) → CANARY_LEAK alarm (crit) → REFUSED → mission HALT.

---

## 5. WRITE phase (research → write) — observability IS the view

The observability panel is the **main element** (not a popup). Layout: a slim left rail +
a wide main column.
- Header strip: `MISSION` (Press Start 2P 10px) · "Lumora Sleep — Launch Post" · right:
  live status `<mAct>_` (e.g. "researching the brief", "writing the post", "revising on
  feedback", "ready to rehearse").
- **Left rail** (`width:230px`, right hairline, faint bg): a `PIPELINE` label then 3 stacked
  nodes RESEARCH / WRITE / REHEARSAL (each a small card: label + status glyph `—/···/GO/NEXT`,
  colored idle/active/done/ready) separated by `↓`. Below: the writer **Robot** (pose `run`)
  + "writer agent". When done: a `▶ REHEARSAL · <n>s` button (auto-advances).
- **Main column** (`flex:1`, scroll): heading `▸ OBSERVABILITY — what the agent searched &
  wrote`. A search-query bar (`▸_ <query>` + engine chip). Then a two-column grid:
  - **SOURCES · what it searched** — fact cards `[f1]…[f4]`: a green `[id]` chip + the fact
    text + a `🔗 domain` source link.
  - **DRAFTS · what it wrote** — draft cards: header (Draft #n · worker · verdict chip
    `✓ passed checks` / `✗ flagged`), body with **flagged spans highlighted** (red bg,
    `border-bottom:2px solid --no`, on the offending phrase), and a list of flag rows
    (`<check> — <reason> "<span>"`, red left border).
- **Streaming choreography** (timed): research starts → facts appear one by one (log `FACT`
  lines) → research GO → write starts → **draft #1 fails** `banned_claims` + `grounding`
  (two alarms, flagged spans shown) → critique routed back (`RETRY`) → **draft #2 passes** →
  write GO → auto-advance. (This fail→revise→pass beat is the "behaviour changes on feedback"
  proof — keep it unmissable.) For the injected-claim scenario the writer's draft passes the
  lexical checks but carries the medical claim through to rehearsal.

---

## 6. REHEARSAL phase — a real X (Twitter) client

The stage **becomes X**. It must read as the real product. Its own palette (§1.1 X block);
do not bleed harness greens into the post/feed.

- **Harness strip** (thin, above the X app): `FAKE X SIM · simulated twin · egress DISABLED
  · nothing is live`. Right side during/after the review: a **scan tally** `✓ <pass>` /
  `✗ <hold>` chips, the verdict pill, and `▶ FINAL APPROVAL · <n>s`.
- **3-column X layout**, centered:
  - **Left nav** (~218px): the `𝕏` glyph; nav rows with simple stroke icons — **Home**
    (bold/active), Explore, Notifications, Messages, Grok, Premium (`Lift off` tag), Profile,
    More; a white **Post** pill button; an account chip at the bottom (Lumora avatar + name +
    @handle + ⋯).
  - **Center** (`flex:1, max 600px`, scroll, x-borders): sticky **For you / Following** tabs
    (active has the blue underline); a composer row ("What's happening?"); then **the post**;
    then the panel **replies**.
  - **Right rail** (~312px): a Search pill; a "Subscribe to Premium" card (Subscribe button);
    a "What's happening" trends card (plausible, non-realtime items).
- **The post**: Lumora avatar (`#5b53b8` "L"), name + a **blue verified check** (a `#1d9bf0`
  circle with a white check) + `@lumorasleep · now`, a `FAKE X SIMULATION` badge (amber).
  Body = the rendered post at 19px Space Grotesk, **with [f#] citations stripped** (clean
  reading copy; citations live in the WRITE observability, not the live tweet). Engagement
  row: reply / retweet / like / views / bookmark stroke icons, muted, **no fake counts**.
- **Replies = the review panel** (one per judge), streamed in (typing dots → reply):
  the judge's **Robot** avatar, name + verified + a **tier chip** (DEEP/STANDARD/LEXICAL) +
  handle (`@gptoss_eval`, `@qwen3_review`, …) + a `PASS`/`HELD` pill, a row of **criteria
  chips** it voted on (a flagged criterion is red), and the one-line comment. Tiering is
  visible: lexical judges vote only `clarity`, standard adds `on_brand`, deep adds
  `no_unsupported_claims`. No scores, no averaging.
- **Auto-scan**: once all replies are in, the view **auto-scrolls and visually scans each
  reply** in turn (highlight that reply's left border green/red), incrementing the top
  `✓/✗` counter as it goes — slow enough to read, then after the last one, **3s → APPROVAL**.
- **Consent banner / HELD**: `✓ UNANIMOUS CONSENT · publish-eligible` (green) or
  `❚❚ HELD · failing: no_unsupported_claims` (amber) or `⚠ ESCALATED · gate failed closed`
  (red). On HELD, the **offending sentence in the post is highlighted** and a giant rotated
  **`HELD`** stamp slams over the tweet (`@keyframes slam`); it never reaches X.

---

## 7. APPROVAL phase (human-in-the-loop) — clean, minimal

Deliberately **understated** (no arcade font, one accent). Centered `max-width:660px`,
Space Grotesk.
- Title "Final approval" (24px 600, `--ink-bright`); subtitle "Nothing reaches X without a
  recorded human decision."
- A small verdict line: a dot + label in the verdict color.
- **The payload card** (hairline, subtle bg): Lumora avatar + name + `@lumorasleep` + char
  count; the post body (citations stripped, HELD spans highlighted if held); a 3-cell footer
  strip (mono): `agent <model>` · `rehearsal egress disabled` · `panel <n>/<n> judges approve`.
- **Decision** (state `decide`): a hint line, then two equal buttons — **Pass & post**
  (green, only enabled when verdict = consent) and **Fail & revise** (red outline). If the
  panel did not consent, PASS is disabled with the note "the harness will not post a held
  draft. send it back instead." (fail-closed).
- **Human-approval toggle OFF**: skip the manual step — on consent it **auto-approves** and
  posts (2s), logging `AUTO`.
- **PASS → posted**: a clean card — a green dot + "Posted · dry run", copy "Approval recorded.
  The post shipped in dry-run with a takedown armed — nothing was sent to the network.", a
  `🔗 x.com/lumorasleep/status/…  (dry-run)` link, and a "Run another mission" button.
- **FAIL → revise**: a textarea "What should the writer change?", a **Log & send back**
  button. On submit: append the note to a visible **critique memory** list, log
  `DENIED` + `LEARN` to telemetry, and show **Re-run write with feedback** (→ WRITE). This is
  the "logs it and learns from it" loop.

---

## 8. Robot sprite (reused component)

A chunky pixel robot, ~36×46px, built from divs (no images): antenna (line + glowing dot),
head (rounded rect, body color), visor (dark rect with two glowing eye dots = trim color),
two arms, body (rounded rect with a 1-letter **monogram** in arcade font = model emblem),
two legs. Props: `body`, `trim`, `visor`, `mono`, `pose`.
**Poses** (drive limb keyframes): `idle` (still), `run` (legs `rl`/arms `ap` alternating +
body `hop` bounce), `swim` (tilt + arm windmill `sw` + leg flutter `fl`), `cycle` (legs
`pedal` full rotation), `climb` (leg flutter), `down` (rotate 82° + grayscale + red eyes),
`win` (slight bob). Each model lineage gets a distinct **body color + monogram** (these are
**original abstract emblems/letters, not the companies' trademarked logos**):
```
GPT-OSS 120B  body #10b981 trim #7df5c8 mono "G"  tier deep
Qwen3 Next80B body #9b6cff trim #cbb0ff mono "Q"  tier deep
GLM 5.1       body #39c5ff trim #aee6ff mono "Z"  tier deep
Mixtral 8x7B  body #ff7a2f trim #ffc199 mono "M"  tier standard
Phi-4 mini    body #7bd64a trim #cdf2ad mono "φ"  tier lexical
Llama 4 Mav.  body #4f8cff trim #aecbff mono "L"  tier lexical
Claude Haiku  body #d98a4f trim #ffcf99 mono "A"  tier deep
SketchyAgent  body #ff4d5e trim #ff9aa3 mono "!"  tier untrusted
```

---

## 9. Animation catalog (CSS @keyframes)
```
blink      opacity 1→0→1 (cursor), 1s steps
pulse      50% opacity .32 (live dots)
hop        translateY 0→-6→0 (run bounce)
rl / ap    leg / arm swing ±30–36° (run gait; legR/armL run reverse)
sw         arm rotate 0→360 (swim windmill)
fl         rotate ±10° (flutter)
pedal      rotate 0→360 (cycle wheels & legs)
spin       rotate 0→360 (bike wheels)
twinkle    opacity .25↔.9 (stars), 4s
wavesurf   background-position-x 0→64px (pool surface)
splash     translateY 0→-26 + fade (swim droplets)
speedline  translateX +40→-160 + fade (sprint)
flyout     translate(0,0)→(180,-160) + fade (canary leak)
bangflash  scale .2→2.6 + fade (gun muzzle / win burst)
pop        scale 0→1.3→1 (trophy)
winsweep   translateX -100%→220% skewX (finish light sweep)
slam       scale 2.3→.9→1 rotate -15° (HELD / cert stamps)
flash      opacity .25↔1 (banners)
float1     translateY 0→-6 (buoys)
```
Keep all gauntlet motion at full speed but **fast** — no slow flourishes (it's recorded for a
demo video).

---

## 10. Data contract (wire to your backend)

Each run is assembled from your persisted events. The frontend only needs these shapes
(map your store → this):

```ts
Run {
  model: string                 // picked agent id
  topic, brand, handle: string
  humanApproval: boolean        // toggle; false ⇒ auto-post on consent
  inject?: 'clean'|'held'|'faulty'

  gauntlet: {                   // Gate 1 / Admission certificate (real data)
    agent, certified: boolean,
    attacks: { attack: string, survived: boolean, evidence?: string }[]  // 5 classes
  }
  research: { query, engine, facts: { [id]: { text, src } } }
  drafts: { attempt, worker, ok, text,
            flags: { check, reason, span }[] }[]   // span ⇒ highlight in body
  panel: {                      // Gate 2 / Rehearsal
    eligible: boolean|null,
    judges: { name, vendor, tier:'lexical'|'standard'|'deep',
              criteria: string[], pass: boolean, comment, heldOn?: string }[]
  }
  rehearsalProof: { egress: 'disabled', byteLen: number }
  post: { text, author, chars, posted, dryRun, held, heldSpans: string[] }
                               // strip /\s*\[f\d\]/ from `text` for the live tweet only
  alarms: { type, severity:'medium'|'high'|'critical', stage, context, action }[]
  timeline: { t, stage, kind, status, tone, text, meta }[]   // → telemetry console
}
```
- The telemetry console renders `timeline` directly (status→color via §1.1).
- The gauntlet game is purely a skin over `gauntlet.attacks` (survived → cleared, false →
  trip + leaked evidence). The breach index = first non-survived attack.
- The X view renders `post` + `panel.judges`; the auto-scan tallies `judges[].pass`.
- Approval PASS calls your existing post(dry-run) endpoint and shows the returned id/link;
  FAIL appends to a critique-memory list and re-invokes the write stage.

---

## 11. Implementation notes / gotchas
- **Inline-style or CSS, but no decorative gradients.** The only gradients are the named
  scene textures (sky, track, pool).
- Drive the **game loop with direct DOM writes** (transform/textContent), keep React state
  for discrete events only (section change, win/lose, score milestones) so re-renders don't
  reset positions.
- Never gate visibility on an entrance animation (background-tab throttling).
- Console must persist and stream across **every** phase — it's the spine and the demo's
  "it actually runs" proof.
- Auto-advance everywhere (3s) with a manual override button; the product's thesis is
  automation with a human gate at Action only.
- Keep one focal point per screen; size type for a projector (no tiny gray-on-gray).
