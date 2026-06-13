"""ITEM 5: the session-level attacks-survived headline. A PURE READ across every
run's persisted `attack` and `certificate` events. The counts must match what the
gauntlet actually recorded -- including a refusal when a sketchy agent is run."""

import pytest

fastapi = pytest.importorskip("fastapi")

from harness import Harness, HarnessHalt, auto_approver
from materials import Store
import ui.server as server


def test_attack_stats_match_persisted_events(tmp_db, launch_mission):
    store = Store(tmp_db)
    # a clean run (all agents survive) + a reject run (a sketchy agent is refused).
    # The reject run halts by design AFTER persisting its attack/certificate events.
    Harness(launch_mission, store, approver=auto_approver).run()
    with pytest.raises(HarnessHalt):
        Harness(launch_mission, store, approver=auto_approver, reject_demo=True).run()

    # ground truth straight from the events
    exp_attacks = exp_survived = exp_refused = 0
    for rid in store.runs():
        for e in store.events(rid):
            if e["kind"] == "attack":
                exp_attacks += 1
                if (e["detail"] or {}).get("survived"):
                    exp_survived += 1
            elif e["kind"] == "certificate" and not e["ok"]:
                exp_refused += 1

    s = server._attack_stats(store)
    assert exp_attacks > 0, "the gauntlet should have fired attacks"
    assert s["attacks"] == exp_attacks
    assert s["survived"] == exp_survived
    assert s["breaches"] == exp_attacks - exp_survived
    assert s["agents_refused"] == exp_refused
    assert exp_refused >= 1, "the reject-demo sketchy agent must be refused"
    assert "sketchy-agent" in s["distinct_agents_refused"]
    assert 0.0 <= s["survival_rate"] <= 1.0
    store.close()


def test_attack_stats_empty():
    import tempfile, os
    db = os.path.join(tempfile.mkdtemp(), "empty.db")
    store = Store(db)
    s = server._attack_stats(store)
    assert s == {"attacks": 0, "survived": 0, "breaches": 0, "agents_refused": 0,
                 "agents_certified": 0, "distinct_agents_refused": [], "survival_rate": None}
    store.close()
