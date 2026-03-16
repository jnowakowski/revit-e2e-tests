"""Shared logic for Graftd ribbon flows.

Strategy: fire-and-check with fallback.
1. Try cached wrapper (instant)
2. Try known path (one children() call)
3. Search (slow but always finds)
"""

import re
import time

from runner.api import RevitAPI


def _find_path_in_json(tree, target, _prefix=""):
    for i, child in enumerate(tree.get("children", [])):
        path = f"{_prefix}.{i}" if _prefix else str(i)
        if (child.get("text") == target.get("text") and
            child.get("type") == target.get("type") and
            child.get("id", "") == target.get("id", "")):
            return path
        deeper = _find_path_in_json(child, target, path)
        if deeper:
            return deeper
    return None


def run_graftd_command(app, main_win, panel_auto_id, cmd_auto_id, result_title_match,
                       timeout=120, screenshots_dir=None):
    api = RevitAPI()
    start = time.time()

    def t():
        return time.time() - start

    def log(msg):
        print(f"[{t():.1f}s] {msg}", flush=True)

    # -- Health -----------------------------------------------------------
    h = api.health()
    if "error" in h:
        log(f"FAIL: Server not reachable. Start: .\\serve.ps1")
        return False, "Server not reachable"

    state = h.get("state")
    log(f"health: {state} mem={h.get('process',{}).get('memory_mb')}MB")

    if state == "not_running":
        log("FAIL: Revit not running. Run: .\\dev-restart-quick.ps1")
        return False, "Revit not running"

    if state == "security_dialog":
        log("Dismissing security dialog...")
        api.click(path="0.3", method="invoke")
        time.sleep(3)

    if state == "loading":
        log("Waiting for project load...")
        for i in range(30):
            time.sleep(3)
            h = api.health()
            if h.get("state") == "ready":
                break
        if h.get("state") != "ready":
            return False, "Revit not ready"

    log(f"ready: {h.get('window','')}")

    # -- Step 1: Graftd tab -----------------------------------------------
    log("click Graftd tab")
    r = api.click(auto_id="Graftd", control_type="Button", method="invoke")
    log(f"  -> clicked={r.get('clicked')} path={r.get('path')} ({r.get('error','')})")
    if not r.get("clicked"):
        return False, f"Graftd: {r.get('error')}"
    time.sleep(1)

    # -- Step 2+3: panel click + flyout command (one-shot server-side) -----
    # Flyout lives ~3s, HTTP round-trips kill it. Server does it all in one call.
    CMD_PATH_IN_FLYOUT = "1.0.0.0"  # ListBox -> DataItem -> Custom -> Button
    log(f"click panel {panel_auto_id} + command (flyout path={CMD_PATH_IN_FLYOUT})")
    r = api._post("/click-panel-command", {
        "panel_auto_id": panel_auto_id,
        "cmd_path": CMD_PATH_IN_FLYOUT,
    })
    log(f"  -> clicked={r.get('clicked')} text={r.get('text')!r} ({r.get('error','')})")
    if not r.get("clicked"):
        # fallback: try old way (separate clicks)
        log(f"  one-shot failed, trying separate clicks...")
        r = api.click(auto_id=panel_auto_id, control_type="Button", method="focus_click")
        if not r.get("clicked"):
            r = api.click(auto_id=panel_auto_id, control_type="Custom", method="focus_click")
        if not r.get("clicked"):
            return False, f"Panel: {r.get('error')}"
        time.sleep(1)
        # try flyout command
        for attempt in range(10):
            first = api.tree(path="0", depth=0)
            aid = first.get("id", "")
            if "SlideOutPanelPopup" in aid:
                r = api.click(path=f"0.{CMD_PATH_IN_FLYOUT}", method="click_input")
                if r.get("clicked"):
                    break
            time.sleep(0.5)
        if not r.get("clicked"):
            return False, f"Command not found in flyout"

    # -- Step 4: wait for result ------------------------------------------
    # Monitor plugin log file instead of polling UIA tree (faster, reliable)
    import os
    log_file = os.path.expanduser(f"~/Documents/AutoDetailViews-{result_title_match}.log")
    log(f"waiting for result (watching {log_file})...")

    # get initial log size to detect new content
    try:
        initial_size = os.path.getsize(log_file)
    except FileNotFoundError:
        initial_size = 0

    deadline = start + timeout
    poll = 0
    last_line = ""
    while time.time() < deadline:
        time.sleep(1)
        poll += 1

        try:
            with open(log_file, "r") as f:
                content = f.read()
        except FileNotFoundError:
            continue

        lines = content.strip().splitlines()
        if not lines:
            continue

        # show progress from log
        current_last = lines[-1]
        if current_last != last_line:
            last_line = current_last
            log(f"  {current_last.strip()}")

        # check if finished
        if "=== Finished" in content or "Elapsed:" in content:
            # parse result from log
            errors = 0
            warnings = 0
            steps = 0
            for line in lines:
                if "[ERROR]" in line:
                    errors += 1
                if "[WARN]" in line:
                    warnings += 1
                if "[INFO]" in line:
                    steps += 1

            # also try reading result dialog (may still be open)
            api._get("/clear-children-cache")
            first = api.tree(path="0", depth=0)
            ft = first.get("text", "")
            result_text = ""
            if result_title_match in ft or "Command Failure" in ft:
                detail = api.tree(path="0", depth=1)
                for c in detail.get("children", []):
                    if c.get("id") in ("ContentText", "MainInstruction"):
                        txt = c.get("text", "")
                        if txt.strip():
                            result_text += txt + " "
                result_text = result_text.strip()

            if not result_text:
                result_text = f"{steps} steps, {warnings} warnings, {errors} errors (from log)"

            log(f"  DONE: {result_text}")

            if "Command Failure" in ft:
                return False, f"Command failure: {result_text}"
            return errors == 0, result_text

        if poll % 10 == 0:
            h = api.health()
            log(f"  [{t():.0f}s] {h.get('state')} mem={h.get('process',{}).get('memory_mb')}MB (poll {poll})")

    return False, "Timeout"
