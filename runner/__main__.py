"""
Revit plugin E2E test runner.

Usage:
    python -m runner --flow generate-elevations
    python -m runner --flow get-details
    python -m runner --flow generate-elevations --timeout 120 --screenshots
"""

import argparse
import os
import sys
import time

from runner import ui
from runner.flows import FLOWS


def main():
    parser = argparse.ArgumentParser(description="Revit E2E test runner")
    parser.add_argument("--flow", required=True, choices=list(FLOWS.keys()),
                        help="Which flow to test")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Timeout in seconds (default: 120)")
    parser.add_argument("--project", type=str, default="Whitesell",
                        help="Project name to match in Revit title")
    parser.add_argument("--screenshots", action="store_true",
                        help="Save screenshots at each step")
    args = parser.parse_args()

    print(f"[runner] flow={args.flow}, timeout={args.timeout}s, project={args.project}")

    # Connect to Revit
    print("[runner] Connecting to Revit...")
    try:
        app, main_win = ui.connect()
    except Exception as e:
        print(f"[runner] FAIL: Could not connect to Revit: {e}")
        sys.exit(2)

    # Dismiss security dialogs before anything else
    dismissed = ui.dismiss_security_dialogs(main_win)
    if dismissed:
        print(f"[runner] Dismissed {dismissed} security dialog(s).")
        time.sleep(2)

    title = main_win.window_text()
    print(f"[runner] Connected: {title}")

    if args.project.lower() not in title.lower():
        print(f"[runner] WARNING: Project '{args.project}' not found in window title.")

    # Screenshots dir
    screenshots_dir = None
    if args.screenshots:
        screenshots_dir = os.path.join(
            os.path.expanduser("~"), "Documents", "revit-e2e-screenshots", args.flow)
        os.makedirs(screenshots_dir, exist_ok=True)

    # Run flow
    flow_fn = FLOWS[args.flow]
    success, result_text = flow_fn(
        app, main_win,
        timeout=args.timeout,
        screenshots_dir=screenshots_dir,
    )

    print()
    if success:
        print(f"PASS: {result_text}")
        sys.exit(0)
    else:
        print(f"FAIL: {result_text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
