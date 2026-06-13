"""M0 acceptance: the engine runs end-to-end on mocks, the writer's first draft
fails a content checkpoint and passes after revision, everything persists, and a
run can be replayed from a checkpoint without re-running prior stages."""

import os

from harness import Harness
from materials import Store


def test_end_to_end_fail_then_pass(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store)
    run_id = h.run()

    events = store.events(run_id)
    write_checks = [e for e in events if e["stage"] == "write" and e["kind"] == "check"]
    # First attempt failed at least one content check...
    assert any(e["ok"] is False for e in write_checks)
    # ...and a later attempt passed all of them (stage:pass present).
    assert any(e["kind"] == "stage" and e["stage"] == "write" and e["name"] == "pass"
               for e in events)

    # The revised, passing draft is what got persisted.
    out = store.load_output(run_id, "write")
    assert out is not None
    assert "guaranteed" not in out["text"].lower()
    assert "[f1]" in out["text"]  # grounded
    store.close()


def test_revision_was_routed_with_feedback(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store)
    run_id = h.run()
    revisions = [e for e in store.events(run_id) if e["kind"] == "revision"]
    assert revisions, "expected at least one revision routed back to the writer"
    assert "failed these checks" in revisions[0]["detail"]["feedback"]
    store.close()


def test_persistence_and_replay(tmp_db, launch_mission):
    # First process: run fully, then 'die'.
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store).run()
    research_before = store.load_output(run_id, "research")
    store.close()

    # Second process: a brand-new Store over the same db file, replay from write.
    store2 = Store(tmp_db)
    h2 = Harness(launch_mission, store2)
    h2.run(run_id=run_id, replay_from="write")

    # research was reused from the store, not re-run (a replay event is logged).
    replay_events = [e for e in store2.events(run_id)
                     if e["kind"] == "replay" and e["stage"] == "research"]
    assert replay_events, "research should be loaded from store on replay"
    assert store2.load_output(run_id, "research") == research_before
    store2.close()
