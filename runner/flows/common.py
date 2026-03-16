"""Shared logic for Graftd ribbon flows.

All Revit interaction goes through the HTTP server (localhost:8520).
Elements are addressed by AutomationId, never by index path.

Flow:
  1. Check /health
  2. Click Graftd tab (auto_id=Graftd)
  3. Click collapsed panel (auto_id=CustomCtrl_%Graftd%{Panel})
  4. Click command in flyout (auto_id=...%{CommandName})
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

    # -- Pre-flight: health check -----------------------------------------
    log("Health check...")
    h = api.health()
    if "error" in h:
        log(f"FAIL: Server not reachable. Start it: .\\serve.ps1")
        return False, "Server not reachable"

    state = h.get("state", "unknown")
    proc = h.get("process", {})
    log(f"Revit state={state} pid={proc.get('pid')} mem={proc.get('memory_mb')}MB "
        f"uptime={proc.get('started_ago')}")

    if state == "not_running":
        log("FAIL: Revit not running. ACTION: .\\dev-restart-quick.ps1")
        return False, "Revit not running"

    if state == "security_dialog":
        log("Security dialog detected. Dismissing via auto_id...")
        r = api.click(path="0.3", method="invoke")
        log(f"  dismiss: {r.get('clicked')}")
        time.sleep(3)
        h = api.health()
        state = h.get("state")
        if state == "security_dialog":
            log("FAIL: Security dialog still present. ACTION: Click 'Always Load' manually")
            return False, "Security dialog"

    if state == "loading":
        log("Revit loading project, waiting...")
        for i in range(30):
            time.sleep(3)
            h = api.health()
            state = h.get("state")
            p = h.get("process", {})
            log(f"  state={state} mem={p.get('memory_mb')}MB")
            if state == "ready":
                break
        if state != "ready":
            log("FAIL: Revit did not finish loading")
            return False, "Revit not ready"

    log(f"Revit ready. window={h.get('window', '?')!r}")

    # -- Step 1: Click Graftd tab -----------------------------------------
    log("Step 1: Click Graftd tab (auto_id=Graftd, type=Button)")
    r = api.click(auto_id="Graftd", control_type="Button", method="invoke")
    if r.get("clicked"):
        log(f"  OK path={r.get('path')}")
    else:
        log(f"  FAIL: {r.get('error')}")
        return False, f"Graftd click: {r.get('error')}"
    time.sleep(1)

    # cache Graftd panel subtree for next steps
    log("  Caching panel tree (ListBox[0] depth=4)...")
    api.tree(path="0", depth=4)
    log("  Cached.")

    # -- Step 2: Click collapsed panel ------------------------------------
    log(f"Step 2: Click panel (auto_id={panel_auto_id}, type=Button)")
    r = api.click(auto_id=panel_auto_id, control_type="Button", method="focus_click")
    if r.get("clicked"):
        log(f"  OK path={r.get('path')}")
    else:
        # try Custom type (panel wrapper)
        log(f"  Button miss, trying Custom...")
        r = api.click(auto_id=panel_auto_id, control_type="Custom", method="focus_click")
        if r.get("clicked"):
            log(f"  OK path={r.get('path')}")
        else:
            log(f"  FAIL: {r.get('error')}")
            return False, f"Panel click: {r.get('error')}"
    time.sleep(1)

    # -- Step 3: Click command in flyout ----------------------------------
    log(f"Step 3: Click command in flyout ({cmd_auto_id})")
    # flyout is ephemeral -- don't use id_map. Find it directly:
    # flyout = first child with "SlideOutPanelPopup" in auto_id
    # command = search within flyout by auto_id
    cmd_clicked = False
    for attempt in range(15):
        # get children[0] and check if it's the flyout
        t0 = time.time()
        tree = api.tree(path="0", depth=4)  # child[0] with depth=4
        dt = time.time() - t0
        aid = tree.get("id", "")
        if "SlideOutPanelPopup" in aid or "PopupRoot" in aid:
            log(f"  Flyout found at [0] ({dt:.1f}s, attempt {attempt+1})")
            # search for command button inside flyout tree (JSON only, no UIA)
            import jmespath
            matches = jmespath.search(
                f"children[].children[].children[?id && contains(id, '{cmd_auto_id}')][]",
                tree
            ) or []
            if not matches:
                # try one more level
                matches = jmespath.search(
                    f"children[].children[].children[].children[?id && contains(id, '{cmd_auto_id}')][]",
                    tree
                ) or []
            if matches:
                # found -- now click by path within flyout
                from server.__main__ import _find_path_in_json
                cmd_path = _find_path_in_json(tree, matches[0])
                if cmd_path:
                    full_path = f"0.{cmd_path}"
                    log(f"  Command at path={full_path}")
                    r = api.click(path=full_path, method="click_input")
                    if r.get("clicked"):
                        log(f"  OK clicked")
                        cmd_clicked = True
                        break
                    else:
                        log(f"  Click failed: {r.get('error')}")
            else:
                log(f"  Flyout open but command not found in tree")
        else:
            if attempt == 0:
                log(f"  Flyout not at [0] (got {aid!r}), polling... ({dt:.1f}s)")
        time.sleep(0.5)

    if not cmd_clicked:
        log(f"  FAIL: {cmd_auto_id} not found after 15 attempts")
        return False, f"Command {cmd_auto_id} not found"

    # -- Step 4: Wait for result dialog -----------------------------------
    log("Step 4: Waiting for result...")
    deadline = start + timeout
    poll_count = 0
    while time.time() < deadline:
        time.sleep(2)
        poll_count += 1
        tree = api.tree(depth=1)
        children = tree.get("children", [])

        for child in children:
            ct = child.get("text", "")
            if result_title_match in ct or "Command Failure" in ct:
                log(f"  Result found: {ct!r} (poll #{poll_count})")

                # get result text from dialog children
                result_idx = None
                for i, c in enumerate(children):
                    t = c.get("text", "")
                    if result_title_match in t or "Command Failure" in t:
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
                log(f"  {result_text}")

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
            log(f"  [{elapsed():.0f}s] state={h.get('state')} mem={p.get('memory_mb')}MB (poll #{poll_count})")

    log(f"FAIL: Result not found after {timeout}s ({poll_count} polls)")
    return False, "Timeout waiting for result"
