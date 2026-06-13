"""LOCKS the three live-demo headline paths exactly as they will be shown.
If any of these breaks, the demo breaks -- treat a failure here as a release
blocker.

  1. clean run        -> reaches a DRY-RUN post (the lie-free post goes out, safely)
  2. --block-demo     -> a medical claim is HELD and NEVER posts
  3. --reject-demo    -> a sketchy agent is REFUSED at Admission and the run halts
"""

import pytest

from harness import Harness, HarnessHalt, auto_approver
from materials import Store


def test_clean_run_reaches_a_dry_run_post(tmp_db, launch_mission):
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    posts = [e for e in store.events(run_id) if e["kind"] == "post"]
    assert posts, "clean run must reach a post"
    assert posts[0]["detail"]["mode"] == "dry_run"
    assert posts[0]["detail"]["live"] is False
    # and the first draft failed a content check, then a later draft passed
    checks = [e for e in store.events(run_id) if e["kind"] == "check" and e["stage"] == "write"]
    assert any(e["ok"] is False for e in checks) and any(e["ok"] for e in checks)
    store.close()


def test_block_demo_holds_and_never_posts(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store, block_demo=True, approver=auto_approver)
    with pytest.raises(HarnessHalt) as ei:
        h.run()
    assert ei.value.alarm.type.value == "ESCALATE_HUMAN"
    assert any(e["kind"] == "panel" and e["ok"] is False for e in store.events(h.last_run_id))
    assert not [e for e in store.events(h.last_run_id) if e["kind"] == "post"], "a HELD post must never go live"
    store.close()


def test_reject_demo_refuses_at_admission_and_halts(tmp_db, launch_mission):
    store = Store(tmp_db)
    h = Harness(launch_mission, store, reject_demo=True, approver=auto_approver)
    with pytest.raises(HarnessHalt) as ei:
        h.run()
    assert ei.value.alarm.type.value == "CERTIFICATION_FAILED"
    cert = [e for e in store.events(h.last_run_id)
            if e["kind"] == "certificate" and e["detail"].get("agent") == "sketchy-agent"]
    assert cert and cert[0]["ok"] is False
    assert not [e for e in store.events(h.last_run_id) if e["kind"] == "post"]
    store.close()
