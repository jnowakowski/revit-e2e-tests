"""Full model scan: dump + all commands + conflict test on every model."""

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from revit_client import RevitClient

MODELS = [
    "Whitesell Residence",
    "Mount Joy Borough",
    "DACHsampleproject",
    "racadvancedsampleproject",
    "racbasicsampleproject",
    "Duplex",
    "Audubon_Architecture_v1",
    "asset_rme_advanced_sample_project",
]

COMMANDS = ["DeepScan", "DoorFrameClassifier", "DoorScheduleResolver", "GetDoorDetails"]
CONFLICT_DOORS = 3


def run_model(client, model, out_dir):
    """Run full test suite on one model. Returns results dict."""
    safe = model.replace(" ", "_")
    results = {"model": model, "timestamp": datetime.utcnow().isoformat(), "steps": {}}

    # 1. Dump
    print(f"  [1] Dump...", end=" ", flush=True)
    dump_resp, dump_data = client.dump_with_data()
    results["steps"]["dump"] = {"ok": dump_resp.get("ok"), "message": dump_resp.get("message", "")}
    if dump_data:
        results["steps"]["dump"]["schedules"] = client.schedule_count(dump_data)
        results["steps"]["dump"]["doors"] = len(client.door_ids(dump_data))
    print(dump_resp.get("message", "?"))
    if dump_data:
        print(f"        Schedules: {client.schedule_count(dump_data)}, Doors: {len(client.door_ids(dump_data))}")

    # 2. Commands (clean run)
    for i, cmd in enumerate(COMMANDS, 2):
        print(f"  [{i}] {cmd}...", end=" ", flush=True)
        resp = client.command(cmd)
        ok = resp.get("ok", False)
        results["steps"][cmd] = {"ok": ok, "message": resp.get("message", "")[:200]}
        print(f"ok={ok}")

    # 3. Conflict test
    step_n = len(COMMANDS) + 2
    if dump_data:
        all_door_ids = client.door_ids(dump_data)
        test_ids = random.sample(all_door_ids, min(CONFLICT_DOORS, len(all_door_ids))) if all_door_ids else []

        if test_ids:
            print(f"  [{step_n}] Seed conflicts on {len(test_ids)} doors...", flush=True)
            seeded = []
            for did in test_ids:
                resp = client.set_param(did, "g_DoorHeadRef", f"CONFLICT_TEST_{int(time.time())}")
                ok = resp.get("ok", False)
                if ok:
                    seeded.append(did)
                    print(f"        id:{did} seeded")
                else:
                    msg = resp.get("message", "?")[:100]
                    print(f"        id:{did} SKIP ({msg})")

            print(f"  [{step_n+1}] Resolver rerun (conflict detection)...", end=" ", flush=True)
            resp = client.resolver()
            ok = resp.get("ok", False)
            results["steps"]["conflict_test"] = {
                "ok": ok,
                "seeded_doors": seeded,
                "seeded_count": len(seeded),
            }
            print(f"ok={ok}, seeded={len(seeded)}")

            # Check JSONL log for conflict count
            log_path = Path.home() / f"Documents/AutoDetailViews-DoorScheduleResolver-{model}.jsonl"
            if log_path.exists():
                last_complete = ""
                for line in open(log_path):
                    if "RESOLVE_COMPLETE" in line:
                        last_complete = line.strip()
                if last_complete:
                    log_data = json.loads(last_complete)
                    msg = log_data.get("msg", "")
                    results["steps"]["conflict_test"]["log"] = msg
                    print(f"        LOG: {msg}")
        else:
            print(f"  [{step_n}] No doors, skipping conflict test")
    else:
        print(f"  [{step_n}] No dump data, skipping conflict test")

    # Save per-model results
    result_file = out_dir / f"{safe}_results.json"
    with open(result_file, "w") as f:
        json.dump(results, f, indent=2)

    return results


def main():
    client = RevitClient()

    # Verify server is up
    try:
        status = client.status()
        build = status["data"]["build"]
        print(f"Server: {build['commit']} - {build['commitMessage']}")
        print(f"Model: {status['data'].get('title', 'none')}")
    except Exception as e:
        print(f"Server not reachable: {e}")
        return

    # Reload plugin
    reload_resp = client.reload_plugin()
    print(f"Plugin reload: {reload_resp.get('reloaded', False)}")
    print()

    # Output directory
    out_dir = Path.home() / f"Documents/Graftd/dumps/scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}\n")

    all_results = []
    for model in MODELS:
        print(f"{'='*60}")
        print(f"=== {model}")
        print(f"{'='*60}")

        current = client.model_title()
        if current != model:
            print("  Closing current model...")
            client.close_model()
            time.sleep(2)
            print(f"  Opening {model}...")
            resp = client.open_model(model)
            if not resp.get("ok"):
                print(f"  SKIP: {resp.get('message', 'failed to open')}")
                all_results.append({"model": model, "skipped": True, "reason": resp.get("message", "")})
                print()
                continue
            time.sleep(3)
            client.reload_plugin()

        results = run_model(client, model, out_dir)
        all_results.append(results)
        print()

    # Close last model
    client.close_model()

    # Summary
    summary_file = out_dir / "scan_summary.json"
    with open(summary_file, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"{'='*60}")
    print(f"=== SCAN COMPLETE")
    print(f"{'='*60}")
    print(f"Results: {out_dir}")
    print(f"Models: {len(all_results)}")
    ok_count = sum(1 for r in all_results if not r.get("skipped"))
    skip_count = sum(1 for r in all_results if r.get("skipped"))
    print(f"  OK: {ok_count}, Skipped: {skip_count}")


if __name__ == "__main__":
    main()
