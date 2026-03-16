# Revit UIA Tree Stability Analysis

Based on 44 snapshots of Revit 2026 main window UI Automation tree,
captured during startup, project loading, security dialog, and idle states.

## Key Finding: Child Indices Shift, AutomationIds Don't

The main window has ~38 children when fully loaded. Their **types and
AutomationIds are stable** across runs. But their **index positions shift**
when modal dialogs (Security, Command Failure) appear as child[0],
pushing everything else down by 1.

Example: `mMainTabs` (ribbon tab bar) is normally at index [4].
When a Security dialog appears, it becomes [5]. After dismiss, back to [4].

**Rule: Never use index-based paths for clicking. Always resolve by AutomationId.**

## What is AutomationId?

`AutomationId` is a string identifier set by the developer on WPF/UIA controls.
In Revit, key UI elements have stable AutomationIds that survive restarts,
dialog popups, and project changes. Anonymous containers (TabControl, Thumb,
Pane) have empty AutomationIds and are only identifiable by index.

In our JSON tree responses, AutomationId appears as the `id` field.

## Stable Elements (always present when Revit loaded)

| AutomationId | Type | What it is |
|---|---|---|
| `ID_ApplicationMenuButton` | Button | File menu |
| `ID_MinimizeButton` | Button | Minimize ribbon |
| `mMainTabs` | Custom | Ribbon tab bar (children are tab Buttons) |
| `startupView` | Custom | Startup/home view |
| `statusBar` | Custom | Bottom status bar |
| `MainWindowTitleControl` | Custom | Title bar |
| `MainWindow_SystemMenuButton` | Custom | Window system menu |
| `MainWindow_SystemButtonsPanel` | Custom | Min/max/close buttons |

## Graftd Plugin Elements (stable after tab click)

| AutomationId | Type | Notes |
|---|---|---|
| `Graftd` | Button | Tab button inside mMainTabs |
| `Graftd_PanelBarScrollViewer` | Custom | Panel container (child of active DataItem) |
| `CustomCtrl_%Graftd%Details` | Custom | Details panel (collapsed) |
| `CustomCtrl_%Graftd%Elevations` | Custom | Elevations panel (collapsed) |
| `CustomCtrl_%Graftd%Elevations_SlideOutPanelPopup_PopupRoot` | Dialog | Flyout after clicking collapsed panel |
| `CustomCtrl_%CustomCtrl_%Graftd%Elevations%GenerateElevationsCmd` | Button | Generate Elevations command |
| `CustomCtrl_%CustomCtrl_%Graftd%Details%GetDetailsCmd` | Button | Get Details command |

## Tree State Transitions

```
Revit starting     -> 0 children (process exists, UI not ready)
Startup view       -> 8 children (minimal UI, no ribbon)
Security dialog    -> N+1 children (dialog inserted at [0], everything shifts)
Fully loaded       -> 38 children (stable layout)
Flyout open        -> 38+1 children (flyout as first child)
Result dialog      -> 38+1 children (result as first child)
```

## What Changes vs What Stays

**Never changes (within a session):**
- AutomationIds of named elements
- Parent-child relationships (mMainTabs always contains tab Buttons)
- Number of ribbon tabs
- Structure of Graftd panel (Details + Elevations)

**Changes on dialog popup/dismiss:**
- Index positions of ALL children (shift by 1)
- Total child count (+1/-1)

**Changes between Revit sessions:**
- ListBox[0] DataItem order may vary (but AutomationIds on panels are stable)
- Number of TabControl/Thumb pairs (docking layout)

## Implications for Automation

1. **Always find elements by AutomationId**, never by index path
2. After dismissing a dialog, re-resolve all cached paths (indices shifted)
3. Cache AutomationId -> current index mapping, invalidate on child count change
4. The `/search?q=Graftd&by=auto_id` endpoint handles this correctly
5. The `/click?path=4.13` approach is fragile -- prefer `/click` with auto_id resolution
