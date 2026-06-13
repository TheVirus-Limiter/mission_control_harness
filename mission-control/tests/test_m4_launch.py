"""M4: the dashboard can list presets/missions and LAUNCH a run (always
non-interactive -> dry-run), plus judges carry a human 'comment'."""

import os
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import ui.server as server  # noqa: E402


def _client(db):
    server.DB_PATH = db
    return TestClient(server.app)


def test_presets_endpoint_lists_the_barrage(tmp_db):
    presets = _client(tmp_db).get("/api/presets").json()
    keys = {p["key"] for p in presets}
    assert {"claude", "gpt", "llama70", "mixtral"} <= keys
    assert len(presets) >= 9  # a real barrage, not a token panel
    # every entry carries what the UI needs
    assert all({"key", "label", "vendor", "provider", "available"} <= set(p) for p in presets)


def test_missions_endpoint(tmp_db):
    missions = _client(tmp_db).get("/api/missions").json()
    keys = {m["key"] for m in missions}
    assert {"launch", "lumora"} <= keys


def test_launch_starts_a_dry_run_and_completes(tmp_db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    c = _client(tmp_db)
    r = c.post("/api/launch", json={"mission": "lumora", "scenario": "normal"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # the background thread runs the mock pipeline; poll until it posts.
    deadline = time.time() + 20
    status = None
    while time.time() < deadline:
        view = c.get(f"/api/runs/{run_id}").json()
        if view.get("timeline"):
            status = view["status"]
            if "posted" in status or status == "halted":
                break
        time.sleep(0.4)
    assert status and "posted" in status
    view = c.get(f"/api/runs/{run_id}").json()
    assert view["post"]["mode"] == "dry_run"               # launches never post live
    assert view["panel"]["judges"] and view["panel"]["judges"][0]["comment"]  # human comment present


def test_launch_unknown_mission_404(tmp_db):
    assert _client(tmp_db).post("/api/launch", json={"mission": "nope"}).status_code == 404


def test_observability_research_and_flagged_drafts(tmp_db):
    """The view surfaces what it searched (query + sources) and what the model
    wrote each attempt with the spans that got flagged."""
    import os
    from harness import Harness, auto_approver
    from materials import Store

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    store = Store(tmp_db)
    run_id = Harness(os.path.join(root, "missions", "lumora.yaml"), store,
                     approver=auto_approver).run()
    store.close()

    view = _client(tmp_db).get(f"/api/runs/{run_id}").json()
    assert view["research"]["query"] and view["research"]["sources"]      # what/where it searched
    drafts = view["drafts"]
    assert len(drafts) >= 2                                                # bad draft + revision
    assert any(d["flags"] for d in drafts)                                # something got flagged
    flagged = next(d for d in drafts if d["flags"])
    assert all("check" in f for f in flagged["flags"])
    assert any(d["ok"] for d in drafts)                                   # the final draft passed
