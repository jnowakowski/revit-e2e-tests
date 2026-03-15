"""Shared logic for Graftd ribbon flows.

All Graftd commands follow the same pattern:
  1. Click Graftd tab
  2. Click collapsed panel (opens flyout)
  3. Click command button in flyout
  4. Wait for result dialog
"""

import re
import time

from runner import ui


def run_graftd_command(app, main_win, panel_auto_id, cmd_auto_id, result_title_match,
                       timeout=120, screenshots_dir=None):
    """Execute a Graftd ribbon command. Returns (success, result_text)."""
    start = time.time()

    def elapsed():
        return time.time() - start

    def log(msg):
        print(f"[{elapsed():.0f}s] {msg}", flush=True)

    def snap(name):
        if screenshots_dir:
            ui.screenshot(main_win, f"{screenshots_dir}/{name}.png")

    snap("01_before_tab")

    # Step 1: Click Graftd tab
    log("Clicking Graftd tab...")
    main_win.set_focus()
    time.sleep(0.3)
    graftd = ui.find_by_text(main_win, "Graftd", depth=2)
    if not graftd:
        log("FAIL: Graftd tab not found.")
        return False, "Graftd tab not found"
    ui.click(graftd, "invoke")
    time.sleep(1)
    log("Graftd tab clicked.")
    snap("02_graftd_tab")

    # Step 2: Click collapsed panel
    log(f"Clicking panel {panel_auto_id}...")
    panel = ui.find_by_auto_id(main_win, panel_auto_id, depth=6)
    if not panel:
        log(f"FAIL: Panel {panel_auto_id} not found.")
        return False, f"Panel {panel_auto_id} not found"

    btn = None
    try:
        for k in panel.children():
            if k.friendly_class_name() == "Button":
                btn = k
                break
    except Exception:
        pass
    ui.click(btn or panel, "click_input")
    time.sleep(1)
    log("Panel clicked.")
    snap("03_flyout")

    # Step 3: Click command in flyout
    log(f"Clicking command {cmd_auto_id}...")
    try:
        first = main_win.children()[0]
    except (IndexError, Exception) as e:
        log(f"FAIL: Could not get first child: {e}")
        return False, "Could not get first child"

    aid = ""
    try:
        aid = first.automation_id()
    except Exception:
        pass

    if "SlideOutPanelPopup" not in aid and "PopupRoot" not in aid:
        log(f"FAIL: Flyout not found. First child: {aid!r}")
        return False, "Flyout not found"

    cmd = ui.find_by_auto_id(first, cmd_auto_id, depth=4)
    if not cmd:
        log(f"FAIL: Command {cmd_auto_id} not found in flyout.")
        return False, f"Command {cmd_auto_id} not found"

    cmd.click_input()  # click_input, not invoke -- set_focus dismisses flyout
    log("Command clicked.")
    snap("04_running")

    # Step 4: Wait for result dialog
    log("Waiting for result...")
    deadline = start + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            first = main_win.children()[0]
            ft = first.window_text()
            if result_title_match in ft or "Command Failure" in ft:
                snap("05_result")
                result_text = ""
                for c in first.children():
                    try:
                        aid = c.automation_id()
                        if aid in ("ContentText", "MainInstruction"):
                            t = c.window_text()
                            if t.strip():
                                result_text += t + " "
                    except Exception:
                        pass
                result_text = result_text.strip()
                log(f"Result: {result_text}")

                close = ui.find_by_auto_id(first, "CommandButton", depth=2)
                if close:
                    ui.click(close, "click_input")

                if "Command Failure" in ft:
                    return False, f"Command failure: {result_text}"

                errors = 0
                match = re.search(r"(\d+)\s+errors?", result_text)
                if match:
                    errors = int(match.group(1))

                return errors == 0, result_text
        except Exception:
            pass

    log("FAIL: Result dialog did not appear.")
    return False, "Timeout waiting for result"
