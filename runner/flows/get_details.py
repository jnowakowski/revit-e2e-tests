"""Flow: Graftd -> Details -> Get Details"""

import re
import time

from runner import ui


def run(app, main_win, timeout=120, screenshots_dir=None):
    """Execute the Get Details flow. Returns (success, result_text)."""
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

    # Step 2: Click collapsed Details panel
    log("Clicking Details panel...")
    details = ui.find_by_auto_id(main_win, "CustomCtrl_%Graftd%Details", depth=6)
    if not details:
        log("FAIL: Details panel not found.")
        return False, "Details panel not found"

    btn = None
    try:
        for k in details.children():
            if k.friendly_class_name() == "Button":
                btn = k
                break
    except Exception:
        pass
    ui.click(btn or details, "click_input")
    time.sleep(1)
    log("Details panel clicked.")
    snap("03_details_flyout")

    # Step 3: Click Get Details in flyout
    log("Clicking Get Details...")
    first = main_win.children()[0]
    aid = ""
    try:
        aid = first.automation_id()
    except Exception:
        pass

    if "SlideOutPanelPopup" not in aid and "PopupRoot" not in aid:
        log(f"FAIL: Flyout not found. First child: {aid!r}")
        return False, "Flyout not found"

    # search for the command button (GetDetailsCmd or similar)
    cmd = ui.find_by_auto_id(first, "GetDetailsCmd", depth=4)
    if not cmd:
        # fallback: any button in the flyout
        cmd = ui.shallow_search(
            first,
            lambda e: e.friendly_class_name() == "Button" and e.window_text().strip(),
            max_depth=4,
        )
    if not cmd:
        log("FAIL: Details command not found in flyout.")
        return False, "Details command not found"

    cmd.click_input()
    log("Get Details clicked.")
    snap("04_generating")

    # Step 4: Wait for result dialog
    log("Waiting for result...")
    deadline = start + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            first = main_win.children()[0]
            ft = first.window_text()
            if "GetDetails" in ft or "AutoDetailViews" in ft:
                snap("05_result")
                result_text = ""
                for c in first.children():
                    try:
                        if c.automation_id() == "ContentText":
                            result_text = c.window_text()
                            break
                    except Exception:
                        pass
                log(f"Result: {result_text}")

                close = ui.find_by_auto_id(first, "CommandButton", depth=2)
                if close:
                    ui.click(close, "click_input")

                errors = 0
                match = re.search(r"(\d+)\s+errors?", result_text)
                if match:
                    errors = int(match.group(1))

                return errors == 0, result_text
        except Exception:
            pass

    log("FAIL: Result dialog did not appear.")
    return False, "Timeout waiting for result"
