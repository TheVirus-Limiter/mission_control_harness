"""M1 acceptance: the three gates.

Covers acceptance criteria:
  2  canary detection (Admission certifies clean, refuses sketchy)
  3  meta_check catches a bad grader -> REVIEWER_FAULT -> escalate, no post
  4  unanimous consent (any single flag -> HELD); no averaging in the code
  5  human hold (no post without a recorded approval)
  6  persistence + replay across gates
  8  safe by default (dry-run, no real post)
"""

import os

import pytest

from gates.action import DryRunXClient, build_x_payload, select_client
from gates.admission import ProvingGround
from gates.rehearsal import Rehearsal
from guardrails import Guardrails
from harness import Harness, HarnessHalt, auto_approver
from materials import Store
from workers.base import Worker
from workers.mock import MockReviewer, MockWriter, SketchyAgent


def _guardrails():
    return Guardrails.from_mission({"guardrails": {"banned_claims": ["cure", "cures", "guaranteed"]}})


# --- criterion 2: canary / admission --------------------------------------
def test_admission_certifies_clean_agent(tmp_db):
    store = Store(tmp_db)
    cert = ProvingGround(store, _guardrails()).certify(MockWriter(), "r1")
    assert cert.certified is True
    assert all(a.survived for a in cert.attacks)
    store.close()


def test_admission_refuses_sketchy_agent(tmp_db):
    store = Store(tmp_db)
    cert = ProvingGround(store, _guardrails()).certify(SketchyAgent(), "r1")
    assert cert.certified is False
    assert cert.failed, "sketchy agent should fail at least one attack"
    # the certificate must carry the actual leaked evidence
    assert any(a.evidence for a in cert.failed)
    store.close()


def test_reject_demo_halts_and_never_posts(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store, reject_demo=True, approver=auto_approver)
    with pytest.raises(HarnessHalt) as ei:
        h.run()
    assert ei.value.alarm.type.value == "CERTIFICATION_FAILED"
    posts = [e for e in store.events(h.last_run_id) if e["kind"] == "post"]
    assert not posts, "a refused agent must never reach a post"
    store.close()


# --- criterion 3: meta_check catches a bad grader -------------------------
def test_block_demo_holds_medical_claim_and_never_posts(tmp_db, launch_mission):
    """The 'blocked post' demo: a legitimate HELD on a real criterion (meta_check
    passes), routed back to the writer, escalates, and never reaches X."""
    store = Store(tmp_db)
    h = Harness(launch_mission, store, block_demo=True, approver=auto_approver)
    with pytest.raises(HarnessHalt) as ei:
        h.run()
    assert ei.value.alarm.type.value == "ESCALATE_HUMAN"
    events = store.events(h.last_run_id)
    assert any(e["kind"] == "panel" and e["ok"] is False for e in events)  # HELD
    assert any(e["kind"] == "alarm" and e["name"] == "CONTENT_REJECTED" for e in events)
    assert not [e for e in events if e["kind"] == "post"], "a held post must never go live"
    # distinguishes this from --faulty-grader: every meta_check PASSED (the hold
    # is a real objection, not a broken grader).
    metas = [e for e in events if e["kind"] == "check" and (e["name"] or "").startswith("meta_check")]
    assert metas and all(m["ok"] for m in metas)
    store.close()


def test_faulty_grader_escalates_and_never_posts(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store, faulty_grader=True, approver=auto_approver)
    with pytest.raises(HarnessHalt) as ei:
        h.run()
    assert ei.value.alarm.type.value == "ESCALATE_HUMAN"
    events = store.events(h.last_run_id)
    assert any(e["kind"] == "alarm" and e["name"] == "REVIEWER_FAULT" for e in events)
    # it retried the judge before escalating (reviewer-retry budget)
    reruns = [e for e in events if e["kind"] == "revision" and "rerun-judge" in (e["name"] or "")]
    assert reruns, "a faulty judge should be re-run before escalation"
    assert not [e for e in events if e["kind"] == "post"], "must not post when the gate is down"
    store.close()


# --- criterion 4: unanimous consent ---------------------------------------
class _HoldingJudge(Worker):
    name = "holding-judge"

    def run(self, task, feedback=None):
        text = task["text"]
        rubric = task["rubric"]
        real_sentence = text.split(".")[0] + "."  # a genuine substring of the post
        criteria = {c: "pass" for c in rubric}
        criteria[rubric[0]] = "fail"
        return {"overall": "fail", "criteria": criteria,
                "reasons": {rubric[0]: "off brand tone"},
                "citations": {rubric[0]: real_sentence}}


def test_unanimous_consent_one_flag_holds(tmp_db):
    store = Store(tmp_db)
    rehearsal = Rehearsal(store, _guardrails(), ["clarity", "on_brand", "no_unsupported_claims"])
    text = "Meet FocusApp, the calm way to get more done. It launches soon."
    # two clean passes + one well-formed HOLD -> unanimous consent fails -> HELD
    judges = [MockReviewer("a"), MockReviewer("b"), _HoldingJudge()]
    result = rehearsal.run("r1", text, judges)
    assert result["eligible"] is False
    assert result["held"] and result["held"][0]["judge"] == "holding-judge"
    store.close()


def test_unanimous_consent_all_pass_is_eligible(tmp_db):
    store = Store(tmp_db)
    rehearsal = Rehearsal(store, _guardrails(), ["clarity", "on_brand", "no_unsupported_claims"])
    judges = [MockReviewer("a"), MockReviewer("b"), MockReviewer("c")]
    result = rehearsal.run("r1", "All good here.", judges)
    assert result["eligible"] is True and not result["held"]
    store.close()


def test_no_averaging_in_aggregation():
    """Defense-critical: the panel aggregates by unanimous consent, never by an
    average or a numeric threshold."""
    import gates.rehearsal as r

    src = open(r.__file__, "r", encoding="utf-8").read().lower()
    for forbidden in ("average", "mean(", "/ len(", "sum(score", "threshold"):
        assert forbidden not in src, f"found averaging-style construct {forbidden!r} in rehearsal.py"


# --- criterion 5: human hold ----------------------------------------------
def test_human_hold_records_approval_before_post(tmp_db, launch_mission):
    store = Store(tmp_db)
    Harness(launch_mission, store, approver=auto_approver).run()
    events = store.events(store.runs()[0])
    approval = [e for e in events if e["kind"] == "approval"]
    post = [e for e in events if e["kind"] == "post"]
    assert approval and approval[0]["ok"] is True
    assert post, "an approved run should record a (dry-run) post"
    assert approval[0]["id"] < post[0]["id"], "approval must be recorded before the post"
    store.close()


def test_human_decline_blocks_post(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store, approver=lambda run_id, rendered: False)
    with pytest.raises(HarnessHalt):
        h.run()
    assert not [e for e in store.events(h.last_run_id) if e["kind"] == "post"]
    store.close()


# --- criterion 6: persistence + replay across gates -----------------------
def test_replay_from_rehearsal(tmp_db, launch_mission):
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    store.close()

    store2 = Store(tmp_db)
    Harness(launch_mission, store2, approver=auto_approver).run(run_id=run_id, replay_from="rehearsal")
    replayed = [e for e in store2.events(run_id)
                if e["kind"] == "replay" and e["stage"] in ("research", "write")]
    assert len(replayed) == 2, "research and write should be reused from the store"
    store2.close()


# --- criterion 8: safe by default -----------------------------------------
def test_dry_run_is_default(tmp_db, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    store = Store(tmp_db)
    client = select_client(store)
    assert isinstance(client, DryRunXClient)
    rec = client.post("r1", build_x_payload("hello world"))
    assert rec["live"] is False and rec["post_id"].startswith("dryrun-")
    store.close()


def test_real_post_blocked_without_all_conditions(tmp_db, monkeypatch):
    # DRY_RUN off but NO credentials -> still dry-run (fail safe).
    monkeypatch.setenv("DRY_RUN", "0")
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    store = Store(tmp_db)
    assert isinstance(select_client(store), DryRunXClient)
    store.close()
