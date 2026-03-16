# Next Session TODO

## Core Problem
Every server restart (watchmedo hot-reload) kills all caches.
First click after restart triggers deep_scan (38s) which is too slow.
If deep_scan fails or returns stale paths, click fails entirely.

## What Works
- curl to server: health (0.1s), resolve (3s), click with warm cache (0.4s)
- /click-panel-command: panel + flyout + command in one shot (4.3s warm)
- Result monitoring via plugin log file (instant, no UIA)

## What Doesn't Work Reliably
- Runner fails on cold start because auto_id resolve requires deep_scan
  which requires children() traversal which is slow and may return stale data
- Hot-reload kills all wrappers, every restart is cold

## Root Cause
We reverse-engineer UIA accessibility tree (meant for screen readers).
Every children() call is a COM roundtrip (3-16s on Revit).
The tree structure shifts when dialogs appear/disappear.

## Proper Fix (from research)
Add HTTP listener INSIDE the Revit plugin (C#):
- PostCommand() executes commands instantly (no UIA)
- AdWindows.dll gives direct access to ribbon (no tree walk)
- Plugin knows its own state (no external polling needed)

This eliminates pywinauto entirely for command execution.
Keep pywinauto server for UI exploration/debugging only.

## Interim Fix (if not doing plugin HTTP)
1. Don't use hot-reload during test runs (start server once, leave it)
2. Pre-warm: after server start, run one manual click sequence to populate caches
3. Runner: if first click fails, tell user to run warm-up command
