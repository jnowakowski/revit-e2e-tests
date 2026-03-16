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

    # -- Step 2: panel click ----------------------------------------------
    log(f"click panel {panel_auto_id}")
    r = api.click(auto_id=panel_auto_id, control_type="Button", method="focus_click")
    if not r.get("clicked"):
        log(f"  Button miss, trying Custom...")
        r = api.click(auto_id=panel_auto_id, control_type="Custom", method="focus_click")
    log(f"  -> clicked={r.get('clicked')} path={r.get('path')} ({r.get('error','')})")
    if not r.get("clicked"):
        return False, f"Panel: {r.get('error')}"
    time.sleep(1)

    # -- Step 3: command in flyout ----------------------------------------
    log(f"click {cmd_auto_id} in flyout")
    cmd_clicked = False
    panel_retried = False
    for attempt in range(15):
        # check child[0] -- flyout appears here
        first = api.tree(path="0", depth=0)
        aid = first.get("id", "")
        if "SlideOutPanelPopup" not in aid and "PopupRoot" not in aid:
            if attempt == 0:
                log(f"  flyout not at [0] yet (got {first.get('type')}:{aid[:30]})")
            # after 3 misses, re-click panel (focus may have been lost)
            if attempt == 3 and not panel_retried:
                panel_retried = True
                log(f"  re-clicking panel...")
                api.click(auto_id=panel_auto_id, control_type="Button", method="focus_click")
                if not r.get("clicked"):
                    api.click(auto_id=panel_auto_id, control_type="Custom", method="focus_click")
                time.sleep(1)
            else:
                time.sleep(0.5)
            continue

        # flyout found -- get its tree and find command
        log(f"  flyout at [0] (attempt {attempt+1})")
        ftree = api.tree(path="0", depth=4)

        import jmespath
        matches = []
        for expr in [
            f"children[?id && contains(id, '{cmd_auto_id}')][]",
            f"children[].children[?id && contains(id, '{cmd_auto_id}')][]",
            f"children[].children[].children[?id && contains(id, '{cmd_auto_id}')][]",
            f"children[].children[].children[].children[?id && contains(id, '{cmd_auto_id}')][]",
        ]:
            matches = jmespath.search(expr, ftree) or []
            if matches:
                break

        if matches:
            cmd_path = _find_path_in_json(ftree, matches[0])
            if cmd_path:
                full_path = f"0.{cmd_path}"
                log(f"  command at {full_path}, clicking...")
                r = api.click(path=full_path, method="click_input")
                log(f"  -> clicked={r.get('clicked')}")
                if r.get("clicked"):
                    cmd_clicked = True
                    break
        else:
            log(f"  flyout open but {cmd_auto_id} not in tree")
        time.sleep(0.5)

    if not cmd_clicked:
        return False, f"Command {cmd_auto_id} not found"

    # -- Step 4: wait for result ------------------------------------------
    log("waiting for result...")
    deadline = start + timeout
    poll = 0
    while time.time() < deadline:
        time.sleep(2)
        poll += 1
        # cheap poll: check child[0] title
        first = api.tree(path="0", depth=0)
        ft = first.get("text", "")
        if result_title_match not in ft and "Command Failure" not in ft:
            if poll % 5 == 0:
                h = api.health()
                log(f"  [{t():.0f}s] {h.get('state')} mem={h.get('process',{}).get('memory_mb')}MB (poll {poll})")
            continue

        # found -- get details
        log(f"  result: {ft!r}")
        detail = api.tree(path="0", depth=1)
        result_text = ""
        for c in detail.get("children", []):
            if c.get("id") in ("ContentText", "MainInstruction"):
                txt = c.get("text", "")
                if txt.strip():
                    result_text += txt + " "
        result_text = result_text.strip()
        log(f"  {result_text}")

        if "Command Failure" in ft:
            return False, f"Command failure: {result_text}"

        errors = 0
        m = re.search(r"(\d+)\s+errors?", result_text)
        if m:
            errors = int(m.group(1))
        return errors == 0, result_text

    return False, "Timeout"
