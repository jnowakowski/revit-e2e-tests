# Revit 2026 UIA Control Map

Zmapowane przez pywinauto (UIA backend). Zrodlo prawdy przy pisaniu automatyzacji.
Eksploracja: `python -m server` w repo `revit-e2e-tests`, potem curl.

## Zasady pracy z drzewem Revita

- **NIGDY** nie uzywaj `descendants()`, `child_window()`, `print_control_identifiers()` na glownym oknie. Wisi.
- `children()` na glownym oknie jest szybkie (~40 elementow).
- `children()` na poszczegolnych dzieciach jest szybkie.
- Szukaj przez `shallow_search()` (BFS po children, poziom po poziomie).
- Indeksy dzieci sie zmieniaja miedzy uruchomieniami. Szukaj po `automation_id` lub `text`, nie po indeksie.

## Glowne okno - dzieci (depth=1)

```
ListBox                              -- Ribbon tab data (DataItems, po jednym na tab)
Button  id=ID_ApplicationMenuButton  -- Menu File
Button  id=ID_MinimizeButton
Button  id=RibbonMiniToggleButton_ContextMenu
Custom  id=mMainTabs                 -- PASEK TABOW RIBBONA (patrz nizej)
Static
TabControl                           -- Panel Properties
TabControl                           -- Panel Project Browser
TabControl                           -- Document tabs (AvalonDock)
TabControl x N                       -- Docking areas (puste)
ListBox x 4                          -- (nieznane)
Pane
Custom  id=startupView
Custom  id=statusBar
Custom  id=MainWindow_SystemMenuButton
Custom  id=MainWindow_ModelBrowserButton
Custom  id=MainWindow_SystemButtonsPanel
Custom  id=MainWindowTitleControl
```

## Pasek tabow ribbona (id: mMainTabs)

Kazdy tab to `Button` z nazwa jako text:

```
Architecture | Structure | Steel | Precast | Systems | Insert | Annotate
Analyze | Massing & Site | Collaborate | View | Manage | Add-Ins | Graftd | Modify
```

Klikniecie: `invoke` na Button.

## Ribbon tab data (ListBox, pierwsze dziecko glownego okna)

Zawiera `DataItem` dla kazdego taba. Wbudowane taby maja text `UIFramework.RvtRibbonTab`.
Plugin tab Graftd ma text `Autodesk.Windows.RibbonTab`.

Po kliknieciu taba, odpowiadajacy DataItem zyskuje dzieci z zawartoscia panelu.

## Graftd tab - zawartosc po aktywacji

```
DataItem: 'Autodesk.Windows.RibbonTab'
  Custom: 'Graftd'  id=Graftd_PanelBarScrollViewer
    ListBox
      DataItem: 'Autodesk.Windows.RibbonPanel'
        Custom: 'Details'  id=CustomCtrl_%Graftd%Details
          Button  id=CustomCtrl_%Graftd%Details    -- collapsed panel icon
            Image  id=mCollapsedPanelImage
      DataItem: 'Autodesk.Windows.RibbonPanel'
        Custom: 'Elevations'  id=CustomCtrl_%Graftd%Elevations
          Button  id=CustomCtrl_%Graftd%Elevations -- collapsed panel icon
            Image  id=mCollapsedPanelImage
```

Oba panele sa collapsed (ribbon za waski). Klik na Button otwiera flyout.

## Flyout popup (po kliknieciu collapsed panelu)

Pojawia sie jako **pierwsze dziecko** glownego okna (typ Dialog).

Przyklad dla Elevations:
```
Dialog  id=CustomCtrl_%Graftd%Elevations_SlideOutPanelPopup_PopupRoot
  Custom  id=mPanelTitleBarInPopup
    Button: 'Elevations'  id=CustomCtrl_%Graftd%Elevations_PanelTitleBar
  ListBox  id=mPopupCollectionView
    DataItem: 'Autodesk.Windows.RibbonButton'
      Custom: 'Generate\nElevations'  id=...GenerateElevationsCmd_RibbonItemContro
        Button: 'Generate\nElevations'  id=...GenerateElevationsCmd
```

Przyklad dla Details (analogicznie):
```
Dialog  id=CustomCtrl_%Graftd%Details_SlideOutPanelPopup_PopupRoot
  ...
    Button: 'Get\nDetails'  id=...GetDetailsCmd
```

**UWAGA:** `set_focus()` na glownym oknie ZAMYKA flyout. Uzywaj `click_input()` bezposrednio.

## Security dialog (przy starcie)

Pojawia sie jesli plugin nie jest podpisany. Tekst: "Security - Unsigned Add-In".
Przycisk: "Always Load" / "Load Once" / "Do Not Load".
Szukaj po tekscie "Always Load" w `shallow_search(win, ..., depth=4)`.

## Result dialog (po zakonczeniu komendy)

Pojawia sie jako **pierwsze dziecko** glownego okna (typ Dialog).

```
Dialog: 'AutoDetailViews - GenerateElevations'   (lub 'AutoDetailViews - GetDetails')
  Static  id=ContentText     -- "Done in 00:00:29. 56 steps, 63 warnings, 0 errors, 0 skipped."
  Button  id=ExpandoButton   -- "See details"
  Button  id=CommandButton_8 -- "Close"
  TitleBar
```

Przy crashu (np. brakujacy config):
```
Dialog: 'Command Failure for External Command'
  Static  id=MainInstruction -- komunikat bledu
  Static  id=ContentText     -- (pusty)
  Button  id=ExpandoButton   -- "Show details"
  Button  id=CommandButton_8 -- "Close"
```

## Click sequence - E2E test

```
1. set_focus() na glowne okno
2. shallow_search(text='Graftd', depth=2) -> invoke
3. sleep 1s
4. shallow_search(auto_id='CustomCtrl_%Graftd%Elevations', depth=6)
     -> znajdz Button child -> click_input
5. sleep 1s
6. sprawdz children()[0] glownego okna - czy auto_id zawiera 'SlideOutPanelPopup'
7. shallow_search(auto_id='GenerateElevationsCmd', depth=4) -> click_input (NIE invoke!)
8. poll children()[0] co 2s - czekaj na dialog z 'GenerateElevations' w tytule
9. przeczytaj ContentText, kliknij CommandButton_8
```

## Wzorce auto_id pluginu Graftd

| Element | auto_id |
|---|---|
| Tab button | `Graftd` |
| Panel scrollview | `Graftd_PanelBarScrollViewer` |
| Panel Details | `CustomCtrl_%Graftd%Details` |
| Panel Elevations | `CustomCtrl_%Graftd%Elevations` |
| Flyout popup | `CustomCtrl_%Graftd%{Panel}_SlideOutPanelPopup_PopupRoot` |
| Command button | `CustomCtrl_%CustomCtrl_%Graftd%{Panel}%{CommandName}` |
| Panel title in flyout | `CustomCtrl_%Graftd%{Panel}_PanelTitleBar` |
