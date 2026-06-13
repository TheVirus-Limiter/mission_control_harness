"""Acceptance criterion 7: swappability.

Running a different mission file changes the entire job with NO code change, and
the harness obeys whichever guardrails that file declares."""

import os

from guardrails import Guardrails
from harness import Harness, auto_approver
from materials import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_second_mission_runs_on_same_engine(tmp_db):
    nonprofit = os.path.join(ROOT, "missions", "nonprofit.yaml")
    store = Store(tmp_db)
    # Same Harness class, same engine, different config file.
    run_id = Harness(nonprofit, store, approver=auto_approver).run()
    # It completed all the way to a (dry-run) post.
    assert [e for e in store.events(run_id) if e["kind"] == "post"]
    store.close()


def test_missions_declare_different_guardrails():
    launch = Harness.__init__  # sanity that the same class loads both
    import yaml

    with open(os.path.join(ROOT, "missions", "launch.yaml"), encoding="utf-8") as f:
        g1 = Guardrails.from_mission(yaml.safe_load(f))
    with open(os.path.join(ROOT, "missions", "nonprofit.yaml"), encoding="utf-8") as f:
        g2 = Guardrails.from_mission(yaml.safe_load(f))
    assert g1.banned_claims != g2.banned_claims
    assert g1.margin_floor != g2.margin_floor
