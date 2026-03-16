"""Shared logic for Graftd ribbon flows.

All Revit interaction goes through the HTTP server (localhost:8520).
Runner never touches pywinauto directly.

Flow:
  1. Check /health -- is Revit ready?
  2. Click Graftd tab
  3. Click collapsed panel (opens flyout)
  4. Click command button in flyout
  5. Wait for result dialog
"""

import re
import time

from runner.api import RevitAPI


def run_graftd_command(app, main_win, panel_auto_id, cmd_auto_id, result_title_match,
                       timeout=120, screenshots_dir=None):
    """Execute a Graftd ribbon command via HTTP server. Returns (success, result_text)."""
    api = RevitAPI()
    start = time.time()

    def elapsed():
        return time.time() - start

    def log(msg):
        print(f"[{elapsed():.1f}s] {msg}", flush=True)

    # ── Pre-flight: health check ────────────────────────────────────
    log("Checking server health...")
    h = api.health()
    if "error" in h:
        log(f"FAIL: Server not reachable. Start it: .\\serve.ps1")
        log(f"  error: {h['error']}")
        return False, "Server not reachable"

    state = h.get("state", "unknown")
    proc = h.get("process", {})
    log(f"Revit state={state} pid={proc.get('pid')} mem={proc.get('memory_mb')}MB "
        f"cpu={proc.get('cpu_pct')}% uptime={proc.get('started_ago')}")

    if state == "not_running":
        log("FAIL: Revit not running.")
        log("  ACTION: .\\dev-restart-quick.ps1")
        return False, "Revit not running"

    if state == "security_dialog":
        log("Security dialog detected. Dismissing...")
        r = api.click(path="0.3", method="invoke")
        log(f"  click result: {r}")
        time.sleep(3)
        # re-check
        h = api.health()
        state = h.get("state", "unknown")
        log(f"  state after dismiss: {state}")
        if state == "security_dialog":
            log("FAIL: Security dialog still present.")
            log("  ACTION: Click 'Always Load' manually in Revit")
            return False, "Security dialog not dismissed"

    if state == "loading":
        log(f"Revit loading project... waiting (mem={proc.get('memory_mb')}MB)")
        for i in range(30):
            time.sleep(3)
            h = api.health()
            state = h.get("state")
            p = h.get("process", {})
            log(f"  [{i*3}s] state={state} mem={p.get('memory_mb')}MB cpu={p.get('cpu_pct')}%")
            if state == "ready":
                break
        if state != "ready":
            log("FAIL: Revit did not finish loading.")
            return False, "Revit not ready"

    if state == "starting":
        log("Revit just started, waiting for UI...")
        time.sleep(10)
        h = api.health()
        state = h.get("state")
        log(f"  state after wait: {state}")

    log(f"Revit ready. Window: {h.get('window', '?')}")

    # ── Step 1: Click Graftd tab ────────────────────────────────────
    log("Step 1: Clicking Graftd tab...")
    t0 = time.time()
    search = api._get("/search?q=Graftd&by=auto_id&depth=2")
    results = search.get("results", [])
    dt = time.time() - t0
    graftd_path = None
    for res in results:
        if res.get("auto_id") == "Graftd" and res.get("type") == "Button":
            graftd_path = res["path"]
            break
    if not graftd_path:
        log(f"  FAIL: Graftd tab not found via /search ({dt:.1f}s)")
        log(f"  search results: {results}")
        return False, "Graftd tab not found"
    log(f"  Found Graftd at path={graftd_path} ({dt:.1f}s)")
    r = api.click(path=graftd_path, method="invoke")
    log(f"  Click: clicked={r.get('clicked')} text={r.get('text')!r}")
    if not r.get("clicked"):
        return False, f"Graftd click failed: {r.get('error')}"
    time.sleep(1)

    # Build cache: fetch Graftd tab content (ListBox[0] -> DataItems -> panels)
    log("  Caching Graftd panel tree...")
    t0 = time.time()
    # ListBox is child[0], Graftd DataItem is typically at index 18
    # Fetch with depth=4 to capture panel buttons
    panel_tree = api.tree(path="0", depth=4)
    dt = time.time() - t0
    log(f"  Cached ListBox tree ({dt:.1f}s)")

    # ── Step 2: Click collapsed panel ───────────────────────────────
    log(f"Step 2: Clicking panel {panel_auto_id}...")
    # search in the cached panel tree via jmespath
    t0 = time.time()
    search = api._get(f"/search?q={panel_auto_id}&by=auto_id&depth=6")
    results = search.get("results", [])
    dt = time.time() - t0
    if not results:
        log(f"  FAIL: Panel {panel_auto_id} not found ({dt:.1f}s)")
        return False, f"Panel {panel_auto_id} not found"
    panel_path = results[0]["path"]
    log(f"  Found panel at path={panel_path} ({dt:.1f}s)")
    # find Button child inside panel (collapsed icon)
    t0 = time.time()
    panel_tree = api.tree(path=panel_path, depth=1)
    btn_path = panel_path
    for i, child in enumerate(panel_tree.get("children", [])):
        if child.get("type") == "Button":
            btn_path = f"{panel_path}.{i}"
            log(f"  Button child at {btn_path}")
            break
    r = api.click(path=btn_path, method="click_input")
    dt2 = time.time() - t0
    log(f"  Click: clicked={r.get('clicked')} text={r.get('text')!r} ({dt2:.1f}s)")
    if not r.get("clicked"):
        return False, f"Panel click failed: {r.get('error')}"
    time.sleep(1)

    # ── Step 3: Find and click command in flyout ────────────────────
    log(f"Step 3: Looking for flyout popup...")
    flyout = None
    for attempt in range(10):
        t0 = time.time()
        tree = api.tree(depth=1)
        dt = time.time() - t0
        children = tree.get("children", [])
        for child in children:
            aid = child.get("id", "")
            if "SlideOutPanelPopup" in aid or "PopupRoot" in aid:
                flyout = child
                log(f"  Flyout found: id={aid[:60]} ({dt:.1f}s, attempt {attempt+1})")
                break
        if flyout:
            break
        if attempt == 0:
            log(f"  Not yet ({dt:.1f}s), polling...")
        time.sleep(1)

    if not flyout:
        log("  FAIL: Flyout did not appear after 10 attempts.")
        log(f"  Children types: {[c.get('type') for c in children[:5]]}")
        return False, "Flyout not found"

    # find command button in flyout
    log(f"  Searching for {cmd_auto_id} in flyout...")
    # get flyout path -- it's in children, find its index
    flyout_idx = None
    for i, child in enumerate(children):
        aid = child.get("id", "")
        if "SlideOutPanelPopup" in aid or "PopupRoot" in aid:
            flyout_idx = i
            break

    if flyout_idx is not None:
        t0 = time.time()
        search = api._get(f"/search?q={cmd_auto_id}&by=auto_id&depth=4&scope={flyout_idx}")
        results = search.get("results", [])
        dt = time.time() - t0
        if results:
            cmd_path = results[0]["path"]
            log(f"  Command found at path={cmd_path} text={results[0].get('text')!r} ({dt:.1f}s)")
            r = api.click(path=cmd_path, method="click_input")
            log(f"  Click result: clicked={r.get('clicked')} ({r.get('method')})")
            if not r.get("clicked"):
                log(f"  FAIL: {r}")
                return False, f"Command click failed: {r.get('error')}"
        else:
            log(f"  FAIL: {cmd_auto_id} not found in flyout. search: {search}")
            return False, f"Command {cmd_auto_id} not found in flyout"
    else:
        log("  FAIL: Could not determine flyout index")
        return False, "Flyout index unknown"

    # ── Step 4: Wait for result dialog ──────────────────────────────
    log("Step 4: Waiting for result dialog...")
    deadline = start + timeout
    poll_count = 0
    while time.time() < deadline:
        time.sleep(2)
        poll_count += 1
        t0 = time.time()
        tree = api.tree(depth=1)
        dt = time.time() - t0
        children = tree.get("children", [])

        for child in children:
            ct = child.get("text", "")
            if result_title_match in ct or "Command Failure" in ct:
                log(f"  Result dialog found: {ct!r} (poll #{poll_count}, {dt:.1f}s)")

                # get details
                result_idx = None
                for i, c in enumerate(children):
                    if result_title_match in c.get("text", "") or "Command Failure" in c.get("text", ""):
                        result_idx = i
                        break

                result_text = ""
                if result_idx is not None:
                    detail = api.tree(path=str(result_idx), depth=1)
                    for c in detail.get("children", []):
                        if c.get("id") in ("ContentText", "MainInstruction"):
                            t = c.get("text", "")
                            if t.strip():
                                result_text += t + " "
                result_text = result_text.strip()
                log(f"  Result text: {result_text}")

                if "Command Failure" in ct:
                    return False, f"Command failure: {result_text}"

                errors = 0
                match = re.search(r"(\d+)\s+errors?", result_text)
                if match:
                    errors = int(match.group(1))
                return errors == 0, result_text

        if poll_count % 5 == 0:
            h = api.health()
            p = h.get("process", {})
            log(f"  [{elapsed():.0f}s] Revit state={h.get('state')} mem={p.get('memory_mb')}MB cpu={p.get('cpu_pct')}% (poll #{poll_count})")

    log(f"FAIL: Result dialog did not appear after {timeout}s ({poll_count} polls).")
    return False, "Timeout waiting for result"
