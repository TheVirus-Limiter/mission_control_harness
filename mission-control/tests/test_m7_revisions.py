"""ITEM 2: the fail->revise->pass beat is visible as a diff. A run with a writer
revision exposes both draft attempts and a computed word-level diff in the
assembled view model (a pure read over the persisted `draft` events)."""

import pytest

fastapi = pytest.importorskip("fastapi")

from harness import Harness, auto_approver
from materials import Store
import ui.server as server


def test_revision_diff_in_view_model(tmp_db, launch_mission):
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    view = server._assemble(store, run_id)

    write_drafts = [d for d in view["drafts"] if d["stage"] == "write"]
    assert len(write_drafts) >= 2, "the bad-then-good writer should leave >=2 drafts"

    revs = [r for r in view["revisions"] if r["stage"] == "write"]
    assert revs, "a write revision should produce a diff"
    rv = revs[0]
    assert rv["from"] == 1 and rv["to"] == 2
    # the diff actually captures change: the banned phrase is removed, new words added
    assert rv["removed"] and rv["added"]
    assert any("ins" == o["t"] for o in rv["ops"]) and any("del" == o["t"] for o in rv["ops"])
    # the feedback that caused the rewrite is attached (the failing draft's flags)
    assert rv["feedback"] and any(f.get("check") for f in rv["feedback"])
    store.close()


def test_word_diff_helper():
    d = server._word_diff("the product is guaranteed to work", "the product works well")
    assert "guaranteed" in d["removed"]
    assert d["added"]
    assert any(o["t"] == "del" for o in d["ops"])
