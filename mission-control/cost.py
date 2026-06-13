"""Cost & latency meter -- a PURE READ over a run's persisted events.

Nothing here calls a model or changes a run. It aggregates the telemetry the
harness already records (per-draft `tokens`/`seconds`, per-judge `_tokens`, and
event timestamps) into a per-run readout: wall-clock, total tokens, an ESTIMATED
dollar cost from a declared price table, and a per-gate breakdown.

The dollar figure is explicitly an estimate: we hold a small, declared table of
blended list prices (USD per 1M tokens) and match it by model-name substring. It
is marked `estimate: true` everywhere it surfaces. It is NOT a billed amount.
"""

from __future__ import annotations

# Declared, blended list-price estimates in USD per 1,000,000 tokens. Blended
# because the harness records a single token total per call (not split in/out).
# These are ballpark public prices for ordering-of-magnitude cost awareness only.
PRICE_PER_1M_USD: dict[str, float] = {
    "claude-opus": 18.0, "opus": 18.0,
    "claude-sonnet": 6.0, "sonnet": 6.0,
    "claude-haiku": 1.5, "haiku": 1.5, "claude": 3.0,
    "gpt-4o-mini": 0.45, "gpt-4o": 7.0, "gpt-4.1": 4.0, "gpt": 2.0,
    "llama": 0.3, "nemotron": 0.4, "mixtral": 0.4, "mistral": 0.4,
    "qwen": 0.3, "phi": 0.2, "gemma": 0.2, "deepseek": 0.5, "gemini": 1.0,
}
DEFAULT_PRICE_PER_1M_USD = 0.5

# Which lifecycle gate each persisted stage belongs to.
STAGE_TO_GATE = {
    "admission": "Admission",
    "research": "Generation", "write": "Generation",
    "rehearsal": "Rehearsal",
    "action": "Action",
}
GATE_ORDER = ["Admission", "Generation", "Rehearsal", "Action"]


def price_per_1m(model: str) -> float:
    """Best-effort blended price for a model/worker name (longest key wins)."""
    m = (model or "").lower()
    best = None
    for key, price in PRICE_PER_1M_USD.items():
        if key in m and (best is None or len(key) > len(best[0])):
            best = (key, price)
    return best[1] if best else DEFAULT_PRICE_PER_1M_USD


def _tokens_of(detail: dict) -> int:
    if not isinstance(detail, dict):
        return 0
    for k in ("tokens", "_tokens"):
        v = detail.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _model_of(event: dict) -> str:
    """The model/worker that did the work for this event."""
    d = event.get("detail") or {}
    if isinstance(d, dict) and d.get("worker"):
        return str(d["worker"])
    # verdict events: the event name is the judge's name
    if event.get("kind") == "verdict":
        return str(event.get("name") or "")
    return ""


def meter(events: list[dict]) -> dict:
    """Aggregate a run's persisted events into the cost/latency readout."""
    if not events:
        return {"present": False}

    ts = [e["ts"] for e in events if e.get("ts") is not None]
    wall = round(max(ts) - min(ts), 2) if len(ts) >= 2 else 0.0

    by_gate: dict[str, dict] = {g: {"tokens": 0, "seconds": 0.0, "cost_usd": 0.0}
                               for g in GATE_ORDER}
    by_model: dict[str, dict] = {}
    total_tokens = 0
    total_cost = 0.0

    for e in events:
        tok = _tokens_of(e.get("detail") or {})
        if tok <= 0:
            continue
        gate = STAGE_TO_GATE.get(e.get("stage"), "Generation")
        model = _model_of(e) or "unknown"
        price = price_per_1m(model)
        cost = tok / 1_000_000 * price

        total_tokens += tok
        total_cost += cost
        by_gate.setdefault(gate, {"tokens": 0, "seconds": 0.0, "cost_usd": 0.0})
        by_gate[gate]["tokens"] += tok
        by_gate[gate]["cost_usd"] = round(by_gate[gate]["cost_usd"] + cost, 6)
        m = by_model.setdefault(model, {"tokens": 0, "cost_usd": 0.0,
                                        "price_per_1m": price})
        m["tokens"] += tok
        m["cost_usd"] = round(m["cost_usd"] + cost, 6)

    # Per-gate wall-time = span of that stage's events (honest, from timestamps).
    for gate in by_gate:
        gts = [e["ts"] for e in events
               if STAGE_TO_GATE.get(e.get("stage")) == gate and e.get("ts") is not None]
        if len(gts) >= 2:
            by_gate[gate]["seconds"] = round(max(gts) - min(gts), 2)

    models = [{"model": k, **v} for k, v in
              sorted(by_model.items(), key=lambda kv: -kv[1]["tokens"])]
    return {
        "present": True,
        "estimate": True,
        "currency": "USD",
        "wall_clock_s": wall,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "by_gate": by_gate,
        "by_model": models,
        "price_basis": "blended list-price estimate, USD per 1M tokens (declared, not billed)",
    }
