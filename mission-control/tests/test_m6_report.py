"""ITEM 1: every governed run produces a complete, exportable AUDIT TRAIL.
The report endpoint returns all required sections (JSON + printable HTML) and
404s for an unknown run."""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from harness import Harness, auto_approver  # noqa: E402
from materials import Store  # noqa: E402
import ui.server as server  # noqa: E402

SECTIONS = ["run_id", "mission", "generated_at", "input", "declared_guardrails",
            "outcome", "admission", "research", "drafts", "checkpoints", "panel",
            "human_approval", "alarms", "timeline"]


def _client(db):
    server.DB_PATH = db
    return TestClient(server.app)


def test_report_has_all_sections(tmp_db, launch_mission):
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    store.close()
    c = _client(tmp_db)

    r = c.get(f"/api/runs/{run_id}/report")
    assert r.status_code == 200
    rep = r.json()
    for k in SECTIONS:
        assert k in rep, f"audit report missing section: {k}"
    assert rep["admission"]["certificates"], "report has no certificate(s)"
    assert rep["checkpoints"], "report has no checkpoint results"
    assert rep["panel"]["judges"], "report has no panel verdicts"
    assert rep["outcome"]["status"].startswith("posted")
    assert rep["human_approval"], "report has no recorded human approval"

    h = c.get(f"/api/runs/{run_id}/report.html")
    assert h.status_code == 200 and "<html" in h.text.lower()
    assert "Audit Report" in h.text and run_id in h.text


def test_report_404_for_unknown(tmp_db):
    assert _client(tmp_db).get("/api/runs/does-not-exist/report").status_code == 404
