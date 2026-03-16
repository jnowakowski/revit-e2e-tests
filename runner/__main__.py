"""
Revit plugin E2E test runner.

Requires the server running: .\\serve.ps1 or python -m server
All Revit interaction goes through HTTP (localhost:8520).

Usage:
    python -m runner --flow generate-elevations
    python -m runner --flow get-details
    python -m runner --flow generate-elevations --timeout 120 --screenshots
"""

import argparse
import os
import sys
import time

from runner.api import RevitAPI
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

    print(f"[runner] flow={args.flow} timeout={args.timeout}s project={args.project}")
    print(f"[runner] server=http://127.0.0.1:8520")

    # Pre-flight: check server
    api = RevitAPI()
    h = api.health()
    if "error" in h:
        print(f"[runner] FAIL: Server not reachable.")
        print(f"[runner] ACTION: Start server first:")
        print(f"[runner]   cd C:\\Users\\orion\\source\\repos\\revit-e2e-tests")
        print(f"[runner]   .\\serve.ps1")
        sys.exit(2)

    state = h.get("state")
    proc = h.get("process", {})
    print(f"[runner] Revit: state={state} pid={proc.get('pid')} mem={proc.get('memory_mb')}MB window={h.get('window', '')!r}")

    # Screenshots dir
    screenshots_dir = None
    if args.screenshots:
        screenshots_dir = os.path.join(
            os.path.expanduser("~"), "Documents", "revit-e2e-screenshots", args.flow)
        os.makedirs(screenshots_dir, exist_ok=True)
        print(f"[runner] screenshots -> {screenshots_dir}")

    # Run flow (common.py handles health checks, security dialogs, etc.)
    flow_fn = FLOWS[args.flow]
    success, result_text = flow_fn(
        None, None,  # app/main_win not used anymore (HTTP only)
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
