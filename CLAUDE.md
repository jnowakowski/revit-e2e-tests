# revit-e2e-tests

End-to-end test runner for Revit plugins using pywinauto. Connects to a running
Revit instance and drives UI automation to exercise plugin flows.

## Usage

Run a test flow directly:

    python -m runner --flow generate-elevations
    python -m runner --flow get-details
    python -m runner --flow generate-elevations --timeout 120 --screenshots

Start the HTTP exploration server (for interactive UI tree inspection):

    python -m server
    python -m server --port 9000

## Structure

- `runner/` -- Test runner. Flows live in `runner/flows/`, UI helpers in `runner/ui.py`.
- `server/` -- HTTP server exposing Revit UI tree for exploration. Endpoints:
  GET /status, GET /tree, GET /windows, GET /inspect, GET /search, POST /click, POST /connect.
- `screenshots/` -- Gitignored. Created at runtime when `--screenshots` is passed.

## Important: pywinauto and Revit

Never call `descendants()`, `child_window()`, or `print_control_identifiers()`
on the Revit main window. The UIA tree is enormous and these calls will hang
indefinitely. Use `runner.ui.shallow_search()` which walks `children()` level
by level with a bounded depth instead.
