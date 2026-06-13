"""PILLAR 3 — Checkpoints.

The inspection library. Every checkpoint is a pure function

    (env, ctx) -> CheckResult(name, ok, evidence)

with an explicit pass/fail and machine-readable evidence. They read the declared
guardrails out of ``ctx`` -- they never invent a rule. With the single exception
that the *review panel* (Gate 2) is LLM-driven, everything here is deterministic
and re-runnable: the same envelope always yields the same verdict.

A reviewer should be able to open this file and see ONLY inspection logic --
no prompts, no model calls, no pipeline orchestration, no worker code.

Checkpoint catalogue
--------------------
schema          required fields for the stage are present and non-empty
grounding       every factual sentence (one containing a digit) carries an
                inline [fN] citation whose id exists in the approved fact set.
                NOTE: this checks PROVENANCE, not truth -- it proves a number was
                traceable to a researched fact, not that the fact is correct.
banned_claims   case-insensitive substring match against guardrails.banned_claims
arithmetic      recompute the total from line_items; must equal stated total
margin          stated_total >= cost * (1 + margin_floor)
meta_check      audit a single model verdict for malformed / hallucinated output
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "evidence": self.evidence}


# ---------------------------------------------------------------------------
# schema: Pydantic models per stage. Empty strings / empty collections fail.
# ---------------------------------------------------------------------------
def _non_empty(v):
    if v is None:
        raise ValueError("must not be null")
    if isinstance(v, (str, bytes)) and not v.strip():
        raise ValueError("must not be empty")
    if isinstance(v, (list, dict, tuple)) and len(v) == 0:
        raise ValueError("must not be empty")
    return v


class ResearchOut(BaseModel):
    topic: str
    facts: dict[str, str]

    _v = field_validator("topic", "facts")(_non_empty)


class WriteOut(BaseModel):
    text: str

    _v = field_validator("text")(_non_empty)


class PriceOut(BaseModel):
    line_items: list[dict]
    stated_total: float
    cost: float

    _v = field_validator("line_items")(_non_empty)


# Map a stage name to the schema that gates it. ctx["schema"] overrides this.
STAGE_SCHEMAS: dict[str, type[BaseModel]] = {
    "research": ResearchOut,
    "write": WriteOut,
    "compose": PriceOut,
}


def schema(env, ctx) -> CheckResult:
    model = ctx.get("schema") or STAGE_SCHEMAS.get(env.stage)
    if model is None:
        # Nothing declared to validate against -> trivially passes.
        return CheckResult("schema", True, {"note": f"no schema declared for stage '{env.stage}'"})
    try:
        model(**env.payload)
        return CheckResult("schema", True, {"model": model.__name__})
    except Exception as e:  # pydantic ValidationError or missing key
        return CheckResult("schema", False, {"model": model.__name__, "errors": str(e)})


# ---------------------------------------------------------------------------
# grounding: factual sentences must cite an approved fact id.
# ---------------------------------------------------------------------------
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_CITATION = re.compile(r"\[([A-Za-z0-9_]+)\]")
_DIGIT = re.compile(r"\d")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip()]


def grounding(env, ctx) -> CheckResult:
    """A sentence is 'factual' if it contains a digit. Every factual sentence
    must contain an inline [fN] citation whose id exists in ctx['facts'].

    This is a PROVENANCE check, not a truth check: it proves the number was
    traceable to a fact in the approved research set, not that the fact is true.
    """
    text = env.payload.get("text", "")
    facts: dict = ctx.get("facts", {}) or {}
    offenders = []
    for sent in _sentences(text):
        if not _DIGIT.search(sent):
            continue  # not a factual sentence
        ids = _CITATION.findall(sent)
        if not ids:
            offenders.append({"sentence": sent, "reason": "factual sentence has no [fN] citation"})
            continue
        unknown = [i for i in ids if i not in facts]
        if unknown:
            offenders.append(
                {"sentence": sent, "reason": f"citation id(s) not in approved facts: {unknown}"}
            )
    ok = not offenders
    return CheckResult(
        "grounding",
        ok,
        {"offenders": offenders, "approved_fact_ids": sorted(facts.keys()),
         "note": "provenance check, not a truth check"},
    )


# ---------------------------------------------------------------------------
# banned_claims: substring match against the declared list.
# ---------------------------------------------------------------------------
def banned_claims(env, ctx) -> CheckResult:
    text = env.payload.get("text", "")
    guardrails = ctx["guardrails"]
    low = text.lower()
    hits = [phrase for phrase in guardrails.banned_claims if phrase.lower() in low]
    return CheckResult("banned_claims", not hits, {"matches": hits})


# ---------------------------------------------------------------------------
# arithmetic: recompute the total from line_items.
# ---------------------------------------------------------------------------
def arithmetic(env, ctx) -> CheckResult:
    items = env.payload.get("line_items", []) or []
    computed = round(sum(float(i.get("amount", 0)) for i in items), 2)
    stated = round(float(env.payload.get("stated_total", 0)), 2)
    ok = abs(computed - stated) < 0.005  # equal to the cent
    return CheckResult(
        "arithmetic",
        ok,
        {"computed_total": computed, "stated_total": stated,
         "line_items": items},
    )


# ---------------------------------------------------------------------------
# margin: stated_total must clear cost * (1 + margin_floor).
# ---------------------------------------------------------------------------
def margin(env, ctx) -> CheckResult:
    guardrails = ctx["guardrails"]
    stated = float(env.payload.get("stated_total", 0))
    cost = float(env.payload.get("cost", 0))
    floor = round(cost * (1 + guardrails.margin_floor), 2)
    ok = stated >= floor - 1e-9
    return CheckResult(
        "margin",
        ok,
        {"stated_total": stated, "cost": cost,
         "margin_floor": guardrails.margin_floor, "required_floor": floor},
    )


# ---------------------------------------------------------------------------
# meta_check: audit a single model verdict. PURE CODE -- no second LLM.
# ---------------------------------------------------------------------------
def meta_check(env, ctx) -> CheckResult:
    """Audit one judge's verdict against the declared rubric and reviewed text.

    The verdict is in ``env.payload``. ``ctx['rubric']`` is the list of declared
    criteria; ``ctx['text']`` is the exact text the judge reviewed.

    Fails if ANY of:
      (a) the criteria keys do not match the declared rubric,
      (b) any criterion value is not "pass"/"fail",
      (c) any "fail" lacks a reason,
      (d) any "fail" cites a span that is NOT a substring of the reviewed text
          (this catches a hallucinating judge), or
      (e) an "overall: pass" contradicts a failing criterion.

    We deliberately use code, not another model, so the auditor of the panel is
    itself deterministic and cannot hallucinate.
    """
    verdict = env.payload or {}
    rubric: list[str] = list(ctx.get("rubric", []))
    text: str = ctx.get("text", "")
    problems: list[str] = []

    criteria = verdict.get("criteria", {})
    if not isinstance(criteria, dict):
        return CheckResult("meta_check", False, {"problems": ["'criteria' is not an object"]})

    # (a) keys must match the declared rubric exactly
    if set(criteria.keys()) != set(rubric):
        problems.append(
            f"criteria keys {sorted(criteria.keys())} do not match declared rubric {sorted(rubric)}"
        )

    # (b) every value must be pass/fail
    for k, v in criteria.items():
        if v not in ("pass", "fail"):
            problems.append(f"criterion '{k}' has non pass/fail value {v!r}")

    reasons = verdict.get("reasons", {}) or {}
    citations = verdict.get("citations", {}) or {}
    for k, v in criteria.items():
        if v != "fail":
            continue
        # (c) a failing criterion must give a reason
        if not str(reasons.get(k, "")).strip():
            problems.append(f"failing criterion '{k}' lacks a reason")
        # (d) it MUST quote a verbatim span from the text. A fail with no citation
        # cannot be verified, so a missing/empty citation is itself a fault --
        # otherwise a hallucinating judge could bypass the audit by omission.
        cite = citations.get(k)
        if not (cite is not None and str(cite).strip()):
            problems.append(f"failing criterion '{k}' provides no citation to verify")
        elif str(cite) not in text:
            problems.append(
                f"failing criterion '{k}' cites a span not present in the reviewed text: {cite!r}"
            )

    # (e) overall must be coherent with the per-criterion verdicts
    overall = verdict.get("overall")
    if overall not in ("pass", "fail"):
        problems.append(f"overall {overall!r} is not pass/fail")
    elif overall == "pass" and any(v == "fail" for v in criteria.values()):
        problems.append("overall 'pass' contradicts a failing criterion")

    ok = not problems
    return CheckResult("meta_check", ok, {"problems": problems})


# ---------------------------------------------------------------------------
# The registry. The engine looks checkpoints up here by the names declared in
# the mission file -- it never imports the functions directly.
# ---------------------------------------------------------------------------
REGISTRY: dict[str, Callable[[Any, dict], CheckResult]] = {
    "schema": schema,
    "grounding": grounding,
    "banned_claims": banned_claims,
    "arithmetic": arithmetic,
    "margin": margin,
    "meta_check": meta_check,
}

# Which alarm type a failed content checkpoint maps to. Used by the engine to
# raise a structured alarm; declared here so the mapping lives beside the checks.
CONTENT_CHECK_ALARM = {
    "schema": "SCHEMA_INVALID",
    "grounding": "UNGROUNDED_CLAIM",
    "banned_claims": "BANNED_CLAIM",
    "arithmetic": "CONTENT_REJECTED",  # an arithmetic mismatch is not a margin breach
    "margin": "MARGIN_BREACH",
}
