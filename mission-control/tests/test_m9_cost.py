"""ITEM 4: the cost + latency meter. A PURE READ over the telemetry the harness
already persists (per-draft tokens/seconds, per-judge _tokens, event timestamps).
It must aggregate totals, attribute tokens to the right gate, and price them off
the declared table (marked an estimate)."""

import pytest

from cost import meter, price_per_1m, DEFAULT_PRICE_PER_1M_USD


def test_meter_aggregates_persisted_meta():
    events = [
        {"ts": 100.0, "stage": "admission", "kind": "certificate", "name": "agent", "detail": {}},
        {"ts": 101.0, "stage": "write", "kind": "draft", "name": "attempt-1",
         "detail": {"worker": "anthropic:claude-haiku-4-5:write", "tokens": 1000, "seconds": 1.2}},
        {"ts": 102.0, "stage": "rehearsal", "kind": "verdict",
         "name": "openai:gpt-4o-mini:review", "detail": {"_tokens": 500}},
        {"ts": 104.0, "stage": "action", "kind": "post", "name": "x", "detail": {}},
    ]
    m = meter(events)
    assert m["present"] and m["estimate"] is True and m["currency"] == "USD"
    assert m["total_tokens"] == 1500
    assert m["wall_clock_s"] == 4.0
    # tokens are attributed to the correct gate lane
    assert m["by_gate"]["Generation"]["tokens"] == 1000
    assert m["by_gate"]["Rehearsal"]["tokens"] == 500
    assert m["by_gate"]["Admission"]["tokens"] == 0
    # cost = tokens/1e6 * declared blended price, summed
    expected = round(1000 / 1e6 * 1.5 + 500 / 1e6 * 0.45, 6)
    assert m["estimated_cost_usd"] == expected
    # per-model rollup, biggest first
    assert m["by_model"][0]["model"] == "anthropic:claude-haiku-4-5:write"
    assert m["by_model"][0]["tokens"] == 1000


def test_price_table_substring_match():
    assert price_per_1m("anthropic:claude-haiku-4-5:write") == 1.5
    assert price_per_1m("openai:gpt-4o-mini:review") == 0.45  # longest key wins
    assert price_per_1m("meta/llama-3.3-70b-instruct") == 0.3
    assert price_per_1m("something-totally-unknown") == DEFAULT_PRICE_PER_1M_USD


def test_meter_empty_run():
    assert meter([]) == {"present": False}


def test_cost_in_view_model(tmp_db, launch_mission):
    fastapi = pytest.importorskip("fastapi")
    from harness import Harness, auto_approver
    from materials import Store
    import ui.server as server

    store = Store(tmp_db)
    run_id = Harness(launch_mission, store, approver=auto_approver).run()
    view = server._assemble(store, run_id)

    cost = view["cost"]
    assert cost["present"] and cost["estimate"] is True
    assert cost["wall_clock_s"] >= 0
    assert set(["Admission", "Rehearsal", "Action"]).issubset(cost["by_gate"].keys())
    # a mock run spends no real tokens -> $0.00 estimate, but the meter still reports
    assert cost["total_tokens"] == 0 and cost["estimated_cost_usd"] == 0.0

    # and it flows into the downloadable audit report
    report = server._report(store, run_id)
    assert report["cost"]["present"] is True
    store.close()
