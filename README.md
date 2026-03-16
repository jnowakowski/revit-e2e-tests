# revit-e2e-tests

End-to-end test runner and UI exploration server for **Revit plugins**, powered by [pywinauto](https://github.com/pywinauto/pywinauto).

Connects to a running Revit instance, drives UI Automation (UIA) to exercise plugin flows, and caches every observation to a local SQLite database for offline analysis.

## Quick start

```bash
pip install pywinauto
```

### Run a test flow

```bash
python -m runner --flow generate-elevations
python -m runner --flow get-details
python -m runner --flow generate-elevations --timeout 120 --screenshots
```

### Start the exploration server

```bash
python -m server                # default port 8520
python -m server --port 9000
python -m server --no-cache     # disable SQLite recording
```

## Server API

### Live endpoints (hit Revit in real-time)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/status` | Connection status and active window title |
| `GET` | `/tree?depth=1&path=4&max=50` | Element tree from root or given path |
| `GET` | `/inspect?path=X.Y.Z` | Detailed element inspection (rect, patterns, etc.) |
| `GET` | `/search?q=term&by=auto_id&depth=3` | Search by `auto_id` or `text` |
| `GET` | `/windows` | List all top-level Revit windows |
| `POST` | `/click` | Click element by path or text |
| `POST` | `/connect` | Reconnect to Revit |

Every response from `/tree`, `/inspect`, and `/search` is automatically recorded to the local SQLite cache, tagged with the current `.rvt` document name and timestamp.

### Cache endpoints (read from SQLite, no Revit needed)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/cache/documents` | List all observed `.rvt` documents with counts |
| `GET` | `/cache/history?document=X&endpoint=/tree&limit=50` | Observation history |
| `GET` | `/cache/search?q=term&document=X` | Full-text search across all cached responses |
| `GET` | `/cache/observation?id=N` | Single observation with full response payload |

## Project structure

```
runner/           Test runner
  flows/          Individual test flows
  ui.py           UI automation helpers (shallow_search, click, etc.)
server/           HTTP exploration server
  __main__.py     Server with live + cache endpoints
  cache.py        Append-only SQLite observation store
  client.py       CLI client
```

## Important: pywinauto and Revit

> **Never** call `descendants()`, `child_window()`, or `print_control_identifiers()` on the Revit main window. The UIA tree is enormous and these calls will hang indefinitely.

Use `runner.ui.shallow_search()` which walks `children()` level by level with a bounded depth instead.

## Cache design

The cache is an **append-only log** of observations -- not a normalized model of the UI tree. This means:

- **No data loss** -- every response is preserved as-is with its timestamp
- **Multi-document** -- observations from different `.rvt` files are tagged separately
- **Offline queryable** -- search across historical snapshots without Revit running
- **Zero drift risk** -- live endpoints always hit Revit; the cache is a read-only history

The database file (`revit_ui_cache.db`) is gitignored and created automatically on first server start.
