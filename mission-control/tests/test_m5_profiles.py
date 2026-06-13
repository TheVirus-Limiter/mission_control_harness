"""Capability-tier strictness profiles: a judge votes on a criterion only if its
tier >= the criterion's required tier; consent is unanimous WITHIN each
criterion's eligible voters; a criterion with no eligible voter fails closed."""

import pytest

from checkpoints import REGISTRY
from gates.rehearsal import Rehearsal, RehearsalEscalation
from guardrails import Guardrails
from materials import Envelope, Store
from models.judges import tier_rank
from workers.base import Worker

TEXT = "This is the Lumora launch post about better rest and calm focus."


def _gr():
    return Guardrails.from_mission({"guardrails": {}})


def _tiers(d):
    return {c: tier_rank(t) for c, t in d.items()}


class Stub(Worker):
    """A judge of a given tier that fails a chosen set of criteria (with a valid,
    in-text citation) and passes the rest."""

    def __init__(self, name, profile, fails=()):
        self.name = name
        self.profile = profile
        self.fails = set(fails)

    def run(self, task, feedback=None):
        rub = task["rubric"]
        crit = {c: ("fail" if c in self.fails else "pass") for c in rub}
        reasons = {c: "no good" for c in rub if crit[c] == "fail"}
        cites = {c: TEXT[:14] for c in rub if crit[c] == "fail"}  # real substring
        overall = "fail" if "fail" in crit.values() else "pass"
        return {"overall": overall, "criteria": crit, "reasons": reasons,
                "citations": cites, "comment": "stub"}


def _rehearsal(tmp_db, rubric, tiers=None):
    return Rehearsal(Store(tmp_db), _gr(), rubric, criterion_tiers=tiers)


# 1) a criterion is HELD iff a judge ASSIGNED to it flags it; a non-assigned judge has no effect
def test_held_only_by_assigned_voter(tmp_db):
    rubric = ["banned_claims", "no_unsupported_claims"]
    tiers = _tiers({"banned_claims": "lexical", "no_unsupported_claims": "deep"})
    # A lexical judge "would" fail the deep criterion -- but it is not assigned it,
    # so it cannot affect the outcome. The deep judge passes everything.
    judges = [Stub("deep", "deep"), Stub("lex", "lexical", fails=["no_unsupported_claims"])]
    res = _rehearsal(tmp_db, rubric, tiers).run("r1", TEXT, judges)
    assert res["eligible"] is True  # the lexical judge never voted on the deep criterion
    assert res["criteria_outcomes"]["no_unsupported_claims"]["voters"] == ["deep"]

    # now the assigned (deep) judge flags it -> HELD
    judges2 = [Stub("deep", "deep", fails=["no_unsupported_claims"]), Stub("lex", "lexical")]
    res2 = _rehearsal(tmp_db, rubric, tiers).run("r2", TEXT, judges2)
    assert res2["eligible"] is False
    assert [h["judge"] for h in res2["held"]] == ["deep"]


# 2) a lexical judge only covers lexical-tier criteria
def test_lexical_judge_scoped_to_lexical_criteria(tmp_db):
    rubric = ["banned_claims", "clarity", "no_unsupported_claims"]
    tiers = _tiers({"banned_claims": "lexical", "clarity": "standard", "no_unsupported_claims": "deep"})
    judges = [Stub("deep", "deep"), Stub("lex", "lexical")]
    res = _rehearsal(tmp_db, rubric, tiers).run("r1", TEXT, judges)
    assert set(res["verdicts"]["lex"]["criteria"].keys()) == {"banned_claims"}
    assert set(res["verdicts"]["deep"]["criteria"].keys()) == set(rubric)
    assert res["assigned"]["lex"] == ["banned_claims"]


# 3) a criterion with zero eligible voters fails CLOSED (no silent pass)
def test_zero_voters_fails_closed(tmp_db):
    rubric = ["no_unsupported_claims"]
    tiers = _tiers({"no_unsupported_claims": "deep"})
    judges = [Stub("lex", "lexical")]  # no deep judge -> nobody can vote
    with pytest.raises(RehearsalEscalation) as ei:
        _rehearsal(tmp_db, rubric, tiers).run("r1", TEXT, judges)
    assert ei.value.alarm.type.value == "CONFIG_ERROR"


# 4) meta_check does not fault a judge for omitting an unassigned criterion
def test_meta_check_audits_only_assigned(tmp_db):
    # a verdict over just the assigned subset is valid against that subset
    verdict = {"overall": "pass", "criteria": {"banned_claims": "pass"}, "reasons": {}, "citations": {}}
    ok = REGISTRY["meta_check"](Envelope("r", "rehearsal", verdict),
                                {"rubric": ["banned_claims"], "text": TEXT})
    assert ok.ok is True
    # and in a tiered run the lexical judge is accepted (not faulted)
    rubric = ["banned_claims", "no_unsupported_claims"]
    tiers = _tiers({"banned_claims": "lexical", "no_unsupported_claims": "deep"})
    res = _rehearsal(tmp_db, rubric, tiers).run("r1", TEXT, [Stub("deep", "deep"), Stub("lex", "lexical")])
    assert "lex" in res["verdicts"]  # seated, not escalated as faulty


# 5) backward compatibility: with no profiles declared, EVERYONE votes on EVERYTHING
def test_backward_compat_no_profiles(tmp_db):
    rubric = ["a", "b"]
    # no criterion_tiers -> every criterion defaults to lexical (tier 0) -> all vote
    judges = [Stub("deep", "deep"), Stub("std", "standard"), Stub("lex", "lexical", fails=["b"])]
    res = _rehearsal(tmp_db, rubric).run("r1", TEXT, judges)
    assert res["eligible"] is False                       # the lexical judge's vote counts
    assert any(h["judge"] == "lex" and h["criterion"] == "b" for h in res["held"])
    assert res["criteria_outcomes"]["b"]["voters"] == ["deep", "std", "lex"]
