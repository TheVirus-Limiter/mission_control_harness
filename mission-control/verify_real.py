"""Run ONE real run end-to-end (real pipeline model + the real six-vendor panel)
and report whether the panel reached UNANIMOUS CONSENT or HELD -- so you can
confirm the real path works before a live demo. Always dry-run (never posts).

  python verify_real.py                 # preset=claude, mission=lumora
  python verify_real.py llama4 missions/launch.yaml

Reads keys from .env. Honest knobs if it escalates too often are printed at the end.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness, HarnessHalt, auto_approver, load_dotenv  # noqa: E402
from materials import Store  # noqa: E402

load_dotenv()
os.environ["DRY_RUN"] = "1"  # this script NEVER posts for real

preset = sys.argv[1] if len(sys.argv) > 1 else "claude"
mission = sys.argv[2] if len(sys.argv) > 2 else os.path.join("missions", "lumora.yaml")
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_real.db")
if os.path.exists(db):
    os.remove(db)

print(f"\n  Running REAL pipeline  preset={preset}  mission={mission}  (dry-run)\n")
store = Store(db)
t0 = time.time()
status = "completed"
run_id = None
try:
    run_id = Harness(mission, store, real=True, preset=preset, approver=auto_approver).run()
except HarnessHalt as h:
    run_id = getattr(h, "alarm", None) and (store.runs()[0] if store.runs() else None)
    status = f"HALT  {h.alarm.type.value}: {h.alarm.context[:90]}"
except Exception as e:  # noqa: BLE001
    status = f"ERROR  {type(e).__name__}: {e}"
dt = round(time.time() - t0, 1)

print(f"  ---- result in {dt}s ----\n")
if run_id:
    certs = [e for e in store.events(run_id) if e["kind"] == "certificate"]
    print("  ADMISSION (who survived the gauntlet):")
    for e in certs:
        d = e["detail"] or {}
        print(f"    {'CERTIFIED' if e['ok'] else 'REFUSED  '}  {d.get('agent')}")
    reh = store.load_output(run_id, "rehearsal")
    if reh:
        print(f"\n  PANEL: {'UNANIMOUS CONSENT (publish-eligible)' if reh.get('eligible') else 'HELD'}")
        for c, o in (reh.get("criteria_outcomes") or {}).items():
            mark = "PASS" if o["passed"] else "HELD"
            print(f"    [{mark}] {c:24s} min_tier={o['min_tier']:8s} voters={o['voters']}")
        for h in reh.get("held", []):
            print(f"      held: {h['judge']} ({h['tier']}) flagged '{h['criterion']}' — {h['reason'][:70]}")
    posted = [e for e in store.events(run_id) if e["kind"] == "post"]
    print(f"\n  POSTED (dry-run): {bool(posted)}")
print(f"  FINAL: {status}\n")
store.close()

print("  Knobs if real runs HOLD/escalate too often (honest, demo-stable):")
print("    * MAX_JUDGES=4        smaller jury -> faster, fewer veto points")
print("    * budgets.writer_revisions: 3   more chances to satisfy the panel")
print("    * judge 'strictness' in models/judges.py: keep deep judges 'normal',")
print("      put any 'strict' posture only on lexical-tier judges (mechanical checks)")
print("    Recommended demo setting: MAX_JUDGES unset (the 6), writer_revisions=3,")
print("    deep judges normal. Use --preset claude for the writer (survives admission).\n")
