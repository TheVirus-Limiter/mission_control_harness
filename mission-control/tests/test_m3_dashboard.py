"""M3: the dashboard is a pure read over the store and assembles the three views.
It also enforces the human hold on its own approve path (no post unless the panel
passed and an approval is recorded)."""

import os

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from harness import Harness, auto_approver  # noqa: E402
from materials import Store  # noqa: E402
import ui.server as server  # noqa: E402


def _seed(db, mission, **kw):
    store = Store(db)
    h = Harness(mission, store, approver=auto_approver, **kw)
    try:
        rid = h.run()
    except Exception:
        rid = h.last_run_id
    store.close()
    return rid


def _client(db):
    server.DB_PATH = db
    return TestClient(server.app)


def test_runs_and_view_assembly(tmp_db, launch_mission):
    rid = _seed(tmp_db, launch_mission)
    c = _client(tmp_db)
    runs = c.get("/api/runs").json()
    assert any(r["run_id"] == rid for r in runs)

    view = c.get(f"/api/runs/{rid}").json()
    assert view["status"].startswith("posted")
    assert view["post"]["text"] and view["post"]["mode"] == "dry_run"
    # three reviewer cards, all PASS, unanimous consent
    assert view["panel"]["eligible"] is True
    assert len(view["panel"]["judges"]) == 3
    assert all(j["verdict"] == "PASS" for j in view["panel"]["judges"])
    # gauntlet carries real per-attack data
    assert view["gauntlet"] and view["gauntlet"][0]["attacks"]


def test_gauntlet_shows_real_breaches(tmp_db, launch_mission):
    rid = _seed(tmp_db, launch_mission, reject_demo=True)
    view = _client(tmp_db).get(f"/api/runs/{rid}").json()
    sketchy = [g for g in view["gauntlet"] if g["agent"] == "sketchy-agent"][0]
    assert sketchy["certified"] is False
    breaches = [a for a in sketchy["attacks"] if not a["survived"]]
    assert breaches and all(b["leaked"] for b in breaches)  # actual leaked output present


def test_dashboard_approve_requires_eligible_panel(tmp_db, launch_mission):
    # A halted (faulty-grader) run never produced an eligible panel -> approve refused.
    rid = _seed(tmp_db, launch_mission, faulty_grader=True)
    c = _client(tmp_db)
    r = c.post(f"/api/runs/{rid}/approve")
    assert r.status_code == 409  # not publish-eligible


def test_404_for_unknown_run(tmp_db):
    c = _client(tmp_db)
    assert c.get("/api/runs/nope").status_code == 404
