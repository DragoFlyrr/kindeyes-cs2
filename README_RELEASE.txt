flashdim -- CS2 flashbang dim overlay
=====================================

Accessibility tool that darkens the screen during CS2 flashbangs for players
with light sensitivity. Nothing is injected into CS2 -- the overlay reads
Valve's official Game State Integration and dims the display externally
(VAC-safe).

INSTALL
-------
1. Extract this whole zip somewhere permanent (Documents, Desktop, etc.)
   -- NOT inside the zip viewer, NOT into Program Files.
2. Double-click setup.bat
   -- It will find your CS2 install, copy one config file, and create a
      "flashdim" shortcut on your desktop.
3. Double-click the desktop shortcut to start the overlay.
4. Launch CS2 in fullscreen-WINDOWED or BORDERLESS mode (not exclusive
   fullscreen -- Windows hides overlays over exclusive fullscreen).

That's it. Throw a flashbang: the screen dims for ~5 seconds.

If Windows SmartScreen blocks flashdim.exe on first launch, click
"More info" -> "Run anyway". The binary is unsigned because code-signing
certs cost $300/year; the source is public.

HOTKEYS
-------
  F8   confirm pulse (test the overlay without a flash)
  F9   toggle detection on/off
  F10  eyelids (hold to manually darken)
  F11  reload (fast restart -- use if overlay gets into a bad state)
  F12  kill

TUNING
------
First run creates gsi_settings.ini next to flashdim.exe. Edit that to
customize:
  GammaCurve   0.65 default. Lower = darkens faster on partial flashes.
  MaxAlpha     0.95 default. Opacity cap (1.0 = fully black).
  OverlayColor #000000 default. Try #1a1a1a if pure black is too jarring.
  DebugHud     set to 1 to see GSI hit count and current flash value.

TROUBLESHOOTING
---------------
Overlay doesn't appear when flashed:
  - Confirm CS2 is NOT in exclusive fullscreen.
  - Press F8 -- if the screen dims, the overlay is running fine.
  - Check gsi_flashdim.log (next to flashdim.exe) for "GSI hit" lines.

Setup can't find CS2:
  - Make sure CS2 is installed and you've launched it at least once.
  - If you moved your Steam library, restart Steam so libraryfolders.vdf
    is up to date, then re-run setup.bat.

UNINSTALL
---------
1. Delete "flashdim.lnk" from your desktop.
2. If you enabled auto-start, open regedit and remove the "flashdim"
   value under HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
3. Delete this folder.
4. (Optional) delete gamestate_integration_flashdim.cfg from
   <Steam>\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\.

LICENSE / SOURCE
----------------
Source: https://github.com/ (see main README)
