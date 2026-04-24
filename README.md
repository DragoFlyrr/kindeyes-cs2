# kindeyes

> **Status: work in progress — not a final release. Expect rough edges.**

CS2 accessibility tool — darkens the screen during flashbangs for players with light sensitivity.

## Quick install (end users)

1. **[Download `flashdim-release.zip`](https://github.com/DragoFlyrr/kindeyes-cs2/releases/latest/download/flashdim-release.zip)** (or browse [all releases](https://github.com/DragoFlyrr/kindeyes-cs2/releases)).
2. Right-click the zip → **Extract All** (anywhere permanent — Documents, Desktop — but **not** inside the zip viewer).
3. Double-click **`setup.bat`**. It finds CS2 via the Steam registry, copies one config file, and makes a desktop shortcut.
4. Double-click the new **flashdim** desktop shortcut. Throw a flashbang in CS2 — screen dims for ~5s.

No Python install, no manual path editing, no launch options. CS2 must run in **fullscreen-windowed** or **borderless** (exclusive fullscreen hides overlays — Windows limitation).

On first launch Windows SmartScreen may show "Windows protected your PC" — click **More info → Run anyway**. The binary is unsigned because code-signing certs cost ~$300/yr; source is in this repo.

**Hotkeys:** F8 confirm pulse · F9 toggle · F10 eyelids · F11 reload · F12 kill

---

## From source (developers)

Two independent implementations live in this repo. Pick one:

### 1. GSI (recommended) — `gsi_flashdim.py`

Uses Valve's official Game State Integration. CS2 pushes `player.state.flashed` (0-255) to a local HTTP listener; overlay alpha is scaled directly from that value. No pixel sampling, no false positives, no bucketed severity profiles.

**Dev setup:**
1. Run `setup.bat` once (auto-detects Steam via registry + `libraryfolders.vdf`, copies the GSI cfg).
2. Run `run_gsi.bat` to start the overlay in source mode (uses `pythonw.exe`). Leave it running in the background.
3. Launch CS2 in **fullscreen-windowed** or **borderless**.

**Stop:** `stop_gsi.bat`

### Building the release bundle

```powershell
py -3.13 -m pip install pyinstaller "setuptools<81"
.\build_hot.bat           # compiles flashdim_hot.dll
powershell -ExecutionPolicy Bypass -File build_release.ps1
```

Produces `flashdim-release.zip` with `flashdim.exe`, `_internal/`, `setup.bat`, `setup.ps1`, `gamestate_integration_flashdim.cfg`, and `README_RELEASE.txt`.

**Tune:** edit `gsi_settings.ini` (auto-created on first run)
- `GammaCurve` — 0.65 default. Lower = darkens faster on partial flashes. Higher = linear.
- `MaxAlpha` — 0.95 default. Opacity cap (1.0 = fully black).
- `OverlayColor` — `#000000` default. Can use dark gray (`#1a1a1a`) if pure black is too jarring.
- `DebugHud` — set to `1` to show a live HUD with GSI hit count and current flash value (useful for verifying CS2 is actually pushing data).

**Files:**
- `gsi_flashdim.py` — main script (stdlib only, no pip installs)
- `flashdim_hot.c` / `flashdim_hot.dll` — native hot path (IOCP listener + Magnification API calls)
- `gamestate_integration_flashdim.cfg` — CS2 config (gets copied into CS2's cfg folder)
- `run_gsi.bat` / `stop_gsi.bat` — launcher / stopper (launcher also works with a built `flashdim.exe`)
- `setup.bat` / `setup.ps1` — registry-aware installer (finds any Steam library), creates shortcut, optional auto-start
- `build_release.ps1` — PyInstaller bundle + zip for end users
- `dump_full.py` — debug tool for inspecting raw GSI payloads
- `gsi_settings.ini` — tunables (auto-created)
- `gsi_flashdim.log` — diagnostic log

## 2. Visual detection (legacy) — `FlashBangColorChanger.ahk`

AutoHotkey v2 script that samples a 9-point pixel grid and fires an overlay on achromatic-bright coverage. Original approach by Brian Vuksanovich.

**Known issues** vs GSI:
- Fires late (screen already whitening before detection)
- Bucketed severity profiles mismatch actual flash intensity
- Pixel tone varies with facing direction / map lighting → inconsistent darkening
- False positives on muzzle flashes / bright scenes

Kept in the repo as a fallback if GSI ever breaks (e.g. Valve changes the API).

**Hotkeys:** same — F9 toggle, F10 eyelids.
**Run:** `run.bat`. **Stop:** `stop.bat`.

---

Built for a user with a physical disability causing extreme light sensitivity. Neither tool modifies CS2 or injects into its process — both are VAC-safe (external overlays only). Do NOT use memory-injection flashbang disablers like `cs2-noflash` on GitHub — those will get you banned.

Original credit: [Brian Vuksanovich](https://www.youtube.com/@brian-vuksanovich).
