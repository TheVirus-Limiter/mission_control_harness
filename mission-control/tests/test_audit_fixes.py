"""Tests for the hardening done after the multi-agent audit: safety guards,
meta_check completeness, citation stripping, mission validation, fail-closed
real mode, the priced checkpoints, and the dashboard takedown route."""

import os

import pytest

from checkpoints import REGISTRY
from gates.action import DryRunXClient, RealXClient, select_client, to_channel
from harness import Harness, HarnessHalt, auto_approver
from materials import Envelope, Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
X_CREDS = {"X_API_KEY": "k", "X_API_SECRET": "s", "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts"}


def _env(payload):
    return Envelope("r1", "rehearsal", payload)


# --- S1: the live-post foot-gun is closed ---------------------------------
def test_auto_approver_can_never_post_live(tmp_db, monkeypatch):
    """DRY_RUN=0 + creds present + a NON-interactive approver must still be a
    dry-run -- a real post requires an interactive human."""
    monkeypatch.setenv("DRY_RUN", "0")
    for k, v in X_CREDS.items():
        monkeypatch.setenv(k, v)
    store = Store(tmp_db)
    # auto_approver.interactive is False -> select_client refuses a live client
    client = select_client(store, "@b", interactive=False)
    assert isinstance(client, DryRunXClient)
    store.close()


def test_select_client_real_only_when_interactive_and_configured(tmp_db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    for k, v in X_CREDS.items():
        monkeypatch.setenv(k, v)
    store = Store(tmp_db)
    assert isinstance(select_client(store, "@b", interactive=True), RealXClient)   # positive branch
    assert isinstance(select_client(store, "@b", interactive=False), DryRunXClient)
    store.close()


def test_full_run_with_yes_stays_dry_run_even_if_live_configured(tmp_db, launch_mission, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    for k, v in X_CREDS.items():
        monkeypatch.setenv(k, v)
    store = Store(tmp_db)
    Harness(launch_mission, store, approver=auto_approver).run()  # auto = non-interactive
    posts = [e for e in store.events(store.runs()[0]) if e["kind"] == "post"]
    assert posts and posts[0]["detail"]["mode"] == "dry_run" and posts[0]["detail"]["live"] is False
    store.close()


# --- meta_check completeness (rules a, b, c, d, e) -------------------------
def test_meta_check_fails_a_fail_without_citation():
    v = {"overall": "fail", "criteria": {"clarity": "fail"}, "reasons": {"clarity": "bad"}, "citations": {}}
    r = REGISTRY["meta_check"](_env(v), {"rubric": ["clarity"], "text": "the post"})
    assert r.ok is False and any("no citation" in p for p in r.evidence["problems"])


def test_meta_check_keys_must_match_rubric():
    v = {"overall": "pass", "criteria": {"clarity": "pass"}, "reasons": {}, "citations": {}}
    r = REGISTRY["meta_check"](_env(v), {"rubric": ["clarity", "on_brand"], "text": "x"})
    assert r.ok is False and any("rubric" in p for p in r.evidence["problems"])


def test_meta_check_values_must_be_binary():
    v = {"overall": "pass", "criteria": {"clarity": "maybe"}, "reasons": {}, "citations": {}}
    r = REGISTRY["meta_check"](_env(v), {"rubric": ["clarity"], "text": "x"})
    assert r.ok is False


def test_meta_check_overall_pass_cannot_contradict_a_fail():
    v = {"overall": "pass", "criteria": {"clarity": "fail"}, "reasons": {"clarity": "b"},
         "citations": {"clarity": "x"}}
    r = REGISTRY["meta_check"](_env(v), {"rubric": ["clarity"], "text": "x"})
    assert r.ok is False and any("contradict" in p for p in r.evidence["problems"])


# --- citation stripping: clean channel, byte-identical ---------------------
def test_to_channel_strips_citation_tokens():
    assert to_channel("Up 90% [f1]. Costs $5 [f2].") == "Up 90%. Costs $5."
    assert to_channel("no tokens here") == "no tokens here"


def test_posted_text_is_clean_but_write_output_keeps_tokens(tmp_db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    store = Store(tmp_db)
    mission = os.path.join(ROOT, "missions", "lumora.yaml")
    run_id = Harness(mission, store, approver=auto_approver).run()
    write = store.load_output(run_id, "write")
    reh = store.load_output(run_id, "rehearsal")
    assert "[f1]" in write["text"]                      # provenance kept for grounding
    assert "[f1]" not in reh["rendered"]["text"]        # stripped from what posts
    store.close()


# --- mission validation ----------------------------------------------------
def test_invalid_mission_halts_cleanly(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("mission: x\nstages: []\n", encoding="utf-8")
    store = Store(str(tmp_path / "d.db"))
    with pytest.raises(HarnessHalt) as ei:
        Harness(str(bad), store)
    assert "stages" in ei.value.alarm.context
    store.close()


def test_mission_with_unknown_checkpoint_halts(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "mission: x\nstages:\n  - name: write\n    type: content\n    worker: writer\n"
        "    checkpoints: [not_a_real_check]\n", encoding="utf-8")
    store = Store(str(tmp_path / "d.db"))
    with pytest.raises(HarnessHalt):
        Harness(str(bad), store)
    store.close()


# --- priced checkpoints actually run (margin_floor is real) ----------------
def test_compose_mission_runs_arithmetic_and_margin(tmp_db):
    store = Store(tmp_db)
    mission = os.path.join(ROOT, "missions", "quote.yaml")
    run_id = Harness(mission, store, approver=auto_approver).run()
    checks = {(e["stage"], e["name"]) for e in store.events(run_id)
              if e["kind"] == "check" and e["ok"]}
    assert ("compose", "arithmetic") in checks
    assert ("compose", "margin") in checks
    assert [e for e in store.events(run_id) if e["kind"] == "post"]
    store.close()


# --- --real fails closed without a real judge panel ------------------------
def test_real_mode_without_keys_fails_closed(tmp_db, launch_mission, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    store = Store(tmp_db)
    with pytest.raises(HarnessHalt) as ei:
        Harness(launch_mission, store, real=True, approver=auto_approver).run()
    assert ei.value.alarm.type.value == "ESCALATE_HUMAN"
    assert "mock judges" in ei.value.alarm.context
    store.close()


# --- dashboard takedown route ---------------------------------------------
def test_dashboard_takedown(tmp_db, launch_mission, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import ui.server as server

    monkeypatch.setenv("DRY_RUN", "1")
    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    store.close()

    server.DB_PATH = tmp_db
    c = TestClient(server.app)
    r = c.post(f"/api/runs/{run_id}/takedown")
    assert r.status_code == 200 and r.json()["ok"] is True
    # a second takedown is refused; the run now shows taken_down
    assert c.post(f"/api/runs/{run_id}/takedown").status_code == 409
    assert c.get(f"/api/runs/{run_id}").json()["taken_down"] is True
