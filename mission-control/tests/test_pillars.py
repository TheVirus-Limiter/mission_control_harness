"""Unit tests for the four pillars in isolation -- they must work without any
worker. (Defense note: the pillars contain no worker logic.)"""

from alarms import Alarm, AlarmType, Severity
from checkpoints import REGISTRY, CheckResult
from guardrails import Guardrails
from materials import Envelope, Store


def _env(stage, payload):
    return Envelope("r1", stage, payload)


def _gr(**kw):
    base = dict(banned_claims=["cure", "guaranteed"], margin_floor=0.5,
               citation_required=True, human_hold_required=True, recipient_allowlist=[])
    base.update(kw)
    return Guardrails(**base)


# --- materials --------------------------------------------------------------
def test_store_roundtrip(tmp_db):
    s = Store(tmp_db)
    s.log("r1", "write", "check", "grounding", ok=False, detail={"x": 1})
    s.save_output(_env("write", {"text": "hello"}))
    assert s.load_output("r1", "write") == {"text": "hello"}
    evs = s.events("r1")
    assert evs[0]["kind"] == "check" and evs[0]["ok"] is False
    assert evs[0]["detail"] == {"x": 1}
    assert s.has_output("r1", "write") and not s.has_output("r1", "nope")
    s.close()


# --- guardrails -------------------------------------------------------------
def test_guardrails_from_mission():
    g = Guardrails.from_mission({"guardrails": {"banned_claims": ["x"], "margin_floor": 0.3}})
    assert g.banned_claims == ["x"] and g.margin_floor == 0.3
    assert g.citation_required is True  # default


# --- alarms -----------------------------------------------------------------
def test_alarm_structured():
    a = Alarm(AlarmType.BANNED_CLAIM, "found 'cure'", "write")
    d = a.as_dict()
    assert d["type"] == "BANNED_CLAIM"
    assert d["severity"] in {"medium", "high", "critical"}
    assert d["recommended_action"]  # non-empty, derived from type
    assert d["stage"] == "write"
    # critical types
    assert Alarm(AlarmType.FORBIDDEN_ACTION, "x", "s").severity == Severity.CRITICAL


# --- checkpoints (deterministic) -------------------------------------------
def test_grounding_flags_uncited_number():
    r = REGISTRY["grounding"](_env("write", {"text": "Sales grew 40% last year."}), {"facts": {"f1": "..."}})
    assert r.ok is False and r.evidence["offenders"]


def test_grounding_passes_with_known_citation():
    r = REGISTRY["grounding"](_env("write", {"text": "Sales grew 40% [f1]."}), {"facts": {"f1": "..."}})
    assert r.ok is True


def test_grounding_rejects_unknown_citation():
    r = REGISTRY["grounding"](_env("write", {"text": "Sales grew 40% [f9]."}), {"facts": {"f1": "..."}})
    assert r.ok is False


def test_banned_claims_case_insensitive():
    r = REGISTRY["banned_claims"](_env("write", {"text": "It is GUARANTEED to work."}), {"guardrails": _gr()})
    assert r.ok is False and "guaranteed" in r.evidence["matches"]


def test_arithmetic_and_margin():
    env = _env("compose", {"line_items": [{"amount": 30.0}, {"amount": 19.0}],
                           "stated_total": 49.0, "cost": 20.0})
    assert REGISTRY["arithmetic"](env, {}).ok is True
    assert REGISTRY["margin"](env, {"guardrails": _gr()}).ok is True  # 49 >= 20*1.5=30
    bad = _env("compose", {"line_items": [{"amount": 5.0}], "stated_total": 5.0, "cost": 20.0})
    assert REGISTRY["margin"](bad, {"guardrails": _gr()}).ok is False


def test_meta_check_catches_hallucinated_citation():
    verdict = {"overall": "fail", "criteria": {"clarity": "fail"},
               "reasons": {"clarity": "bad"}, "citations": {"clarity": "NOT IN TEXT"}}
    r = REGISTRY["meta_check"](_env("rehearsal", verdict),
                               {"rubric": ["clarity"], "text": "the real post"})
    assert r.ok is False and any("not present" in p for p in r.evidence["problems"])


def test_meta_check_passes_clean_verdict():
    verdict = {"overall": "pass", "criteria": {"clarity": "pass"}, "reasons": {}, "citations": {}}
    r = REGISTRY["meta_check"](_env("rehearsal", verdict), {"rubric": ["clarity"], "text": "x"})
    assert r.ok is True
