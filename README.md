# flashbangcolorchanger

> **Status: work in progress — not a final release. Expect rough edges.**

CS2 accessibility tool — darkens the screen during flashbangs for players with light sensitivity.

Two independent implementations live in this repo. Pick one:

## 1. GSI (recommended) — `gsi_flashdim.py`

Uses Valve's official Game State Integration. CS2 pushes `player.state.flashed` (0-255) to a local HTTP listener; overlay alpha is scaled directly from that value. No pixel sampling, no false positives, no bucketed severity profiles.

**Setup:**
1. Run `install_gsi_cfg.bat` once (copies the config into CS2's cfg folder). If it can't find your Steam install, copy `gamestate_integration_flashdim.cfg` manually into `<SteamLibrary>\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\`.
2. Run `run_gsi.bat` to start the overlay. Leave it running in the background.
3. Launch CS2 in **fullscreen-windowed** or **borderless** (exclusive fullscreen hides all GDI overlays — standard Windows limitation).

**Hotkeys:**
- **F9** — toggle detection on/off
- **F10** — manual eyelids (hold key to darken)

**Stop:** `stop_gsi.bat`

**Tune:** edit `gsi_settings.ini` (auto-created on first run)
- `GammaCurve` — 0.65 default. Lower = darkens faster on partial flashes. Higher = linear.
- `MaxAlpha` — 0.95 default. Opacity cap (1.0 = fully black).
- `OverlayColor` — `#000000` default. Can use dark gray (`#1a1a1a`) if pure black is too jarring.
- `DebugHud` — set to `1` to show a live HUD with GSI hit count and current flash value (useful for verifying CS2 is actually pushing data).

**Files:**
- `gsi_flashdim.py` — main script (stdlib only, no pip installs)
- `gamestate_integration_flashdim.cfg` — CS2 config (gets copied into CS2's cfg folder)
- `run_gsi.bat` / `stop_gsi.bat` — launcher / stopper
- `install_gsi_cfg.bat` — auto-installs the cfg into CS2
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
