---
name: Flashbang Color Changer
description: CS2 accessibility overlay at C:/Users/Drago/flashbangcolorchanger — darkens screen during flashbangs for user's light-sensitivity disability
type: project
originSessionId: c9623a85-c054-4e28-94cc-d2801cd64cf6
---
## Project
`C:/Users/Drago/flashbangcolorchanger` — AutoHotkey v2 script that detects CS2 flashbangs via combined audio+visual analysis and shows a darkening overlay. Accessibility tool for a user with extreme sensitivity to white light.

## Why
User has a physical disability causing extreme light sensitivity. Standard gamma/brightness settings don't solve it; needs sub-second reactive darkening tied specifically to flashbangs (not generic bright screens), without affecting game input or risking VAC.

## Files
- `FlashBangColorChanger.ahk` — main script (AHK v2, not v1)
- `run.bat` — launcher (`start "" "%ProgramFiles%\AutoHotkey\v2\AutoHotkey64.exe" "%~dp0FlashBangColorChanger.ahk"`)
- `stop.bat` — PowerShell-based kill targeting only this script's AHK process
- `settings.ini` — INI config (auto-created; delete to reset defaults)
- `flashbang.log` — diagnostic log (deleted on each restart for fresh capture)

## Restart recipe
```bash
cd "/c/Users/Drago/flashbangcolorchanger" && cmd //c ".\\stop.bat" && rm -f flashbang.log && cmd //c ".\\run.bat"
```

## Architecture (as of 2026-04-23)
**Audio gate** (REQUIRED for fire) — WASAPI IAudioMeterInformation peak meter, 8-channel surround:
- Per-channel peak threshold: **0.30** (floor for starting sustain streak)
- Bang peak threshold: **0.45** (streak must contain at least one peak this loud)
- Sustain duration: **120ms** (filters transients like taps/single gunshots)
- Release floor: **peak × 0.55** (hysteresis for streak break)
- Confirmation window: **2000ms** (matches tinnitus duration; visual can land late)
- Omni-ratio check: **disabled** (flashbang audio is NOT truly omnidirectional in surround mix; peak comes from some channels, others stay silent)

**Visual gate** — GDI GetPixel on cached screen DC, 9-point grid at ±800px/±600px from center:
- Per-pixel threshold: `min(R,G,B) >= 100 && (max-min) <= 30` (achromatic-bright)
- QuickDetect: **5-of-9** points must be white (muzzle flashes hit ~1-2 points, real flashbangs hit all 9)
- Persistence: **10ms** of continuous 5-of-9 before firing (rejects 1ms flickers)

**Peak-hunt / profile selection** — 150ms after fire, samples 9-point grid to determine flash severity:
- 9/9 hits = 100% → 1.88s hold + 2.99s fade (0-53° direct)
- 8/9 = 89% → 0.45s hold + 2.95s fade (53-72°)
- 7/9 = 78% → 0.08s hold + 1.87s fade (72-101°)
- ≤6/9 → 0.08s hold + 0.87s fade (101-180° glancing)
- **No abort on 0% peak** — if audio+visual already passed, trust and run glancing profile

## Hotkeys
- **F9** — toggle detection on/off
- **F10** — eyelids (hold overlay indefinitely)

## Tuning history / lessons learned
- **2-of-3 center detection** was too trigger-happy (muzzle flashes, UI elements fired it)
- **6-of-9** was too strict (glancing flashes missed)
- **5-of-9 + 10ms persistence** is current sweet spot
- **Omni-ratio gate** broke everything on 8-channel surround — always 0.00 because rear channels stay silent during flashbangs. Disabled entirely; visual gate handles false positives.
- **Peak bang threshold 0.70** missed user's flashbangs which peak at 0.49-0.70 depending on master volume. Lowered to 0.45; wall-bounce impacts max at ~0.50, gunfire transients fail sustain.
- **Peak-hunt abort on 0% coverage** caused "blinks twice then cuts off" — flashbang whitens center but not corners of 9-point grid. Removed.
- **Audio pass check inside `audioMax >= threshold` block** was a bug: pass never fires if peak dips below threshold at the 250ms-sustain moment (tinnitus peak modulates). Fixed by running pass check every tick during active streak.
- **2-second delay** was `IsFlashPixel` threshold 120 being too strict — screen only reached ~100-115 brightness during ramp-up. Lowered to 100.
- **settings.ini caches old defaults** — when defaults change, delete settings.ini to pick up new values
- **AHK v2 `SetBatchLines(-1)` doesn't exist** (v1 only)
- **Compositor DWM floor** (~4-16ms depending on refresh rate) is a hard latency limit — sub-frame detection requires DirectX hook = VAC risk

## Critical path
1. `timeBeginPeriod(1)` — real 1ms SetTimer delivery
2. `GetDC(0)` cached screen DC — avoid re-acquiring per poll
3. `ProcessSetPriority("High")`
4. `Critical` section + `Sleep(-1)` yield pattern in PollLoop
5. `SetLayeredWindowAttributes` flipped FIRST in StartFlash (before any logging)

## Common pitfalls when editing
- Always restart via `.\stop.bat && rm -f flashbang.log && .\run.bat` to get fresh log
- Log lines can appear out of order in the file (FileAppend non-atomic under load) — use tick values, not line order
- Every flashbang is different: user's master volume, look angle, and map lighting all affect the audio peak and visual coverage. Don't tune to a single test case; target the envelope.
