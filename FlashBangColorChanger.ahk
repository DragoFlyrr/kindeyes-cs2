#Requires AutoHotkey v2.0
#SingleInstance Force

iniFile := A_ScriptDir "\settings.ini"
logFile := A_ScriptDir "\flashbang.log"
if !FileExist(iniFile) {
    IniWrite("F9",       iniFile, "Settings", "HotkeyToggle")
    IniWrite("F10",      iniFile, "Settings", "HotkeyEyelids")
    IniWrite("255",      iniFile, "Settings", "Transparency")
    IniWrite("1",        iniFile, "Settings", "CheckInterval")
    IniWrite("70",       iniFile, "Settings", "CoverageThreshold")
    IniWrite("520",      iniFile, "Settings", "PeakHuntMs")
    IniWrite("cs2.exe",  iniFile, "Settings", "TargetExe")
    IniWrite("0x000000", iniFile, "Settings", "BkColor")
}

hkToggle          := IniRead(iniFile, "Settings", "HotkeyToggle", "F9")
hkEyelids         := IniRead(iniFile, "Settings", "HotkeyEyelids", "F10")
transparency      := Integer(IniRead(iniFile, "Settings", "Transparency", 255))
checkInterval     := Integer(IniRead(iniFile, "Settings", "CheckInterval", 1))
coverageThreshold := Integer(IniRead(iniFile, "Settings", "CoverageThreshold", 70))
peakHuntMs        := Integer(IniRead(iniFile, "Settings", "PeakHuntMs", 520))
targetExe         := IniRead(iniFile, "Settings", "TargetExe", "cs2.exe")
bkColor           := IniRead(iniFile, "Settings", "BkColor", "0x000000")

targetColor := 0xFFFFFF
tolerance   := 12
step        := 1920

DllCall("winmm\timeBeginPeriod", "UInt", 1)
hScreenDC := DllCall("GetDC", "Ptr", 0, "Ptr")
ProcessSetPriority("High")

cs2Active      := false
cs2CheckTick   := 0

; ======== Audio peak meter (WASAPI IAudioMeterInformation) ========
; Audio is a REQUIRED gate: fire overlay only when visual AND audio both confirm within ~500ms.
; Flashbang audio signature = loud peak (proximity) + omnidirectional channels (not footstep/voice).
audioMeter            := 0
audioConfirmedUntil   := 0
audioPeakThreshold    := 0.30   ; peak on ANY channel (sustain floor)
audioPeakBangThreshold := 0.45  ; ADDITIONALLY: streak must contain at least one peak this loud (the bang itself)
audioOmniRatio        := 0.00   ; surround audio isn't truly omnidirectional — disabled
audioConfirmMs        := 2000   ; how long audio confirmation stays valid (tinnitus holds for seconds)
audioSustainMs        := 120    ; peak must stay above threshold for this long (tinnitus, not taps)
audioSustainReleaseRatio := 0.55 ; peak must drop below threshold*this to break sustain streak
DllCall("ole32\CoInitializeEx", "Ptr", 0, "UInt", 2)
try {
    clsidMMDE := Buffer(16, 0)
    iidMMDE   := Buffer(16, 0)
    iidIAM    := Buffer(16, 0)
    DllCall("ole32\CLSIDFromString", "WStr", "{BCDE0395-E52F-467C-8E3D-C4579291692E}", "Ptr", clsidMMDE)
    DllCall("ole32\CLSIDFromString", "WStr", "{A95664D2-9614-4F35-A746-DE8DB63617E6}", "Ptr", iidMMDE)
    DllCall("ole32\CLSIDFromString", "WStr", "{C02216F6-8C67-4B5B-9D00-D008E73E0064}", "Ptr", iidIAM)
    pEnum := 0
    hrEnum := DllCall("ole32\CoCreateInstance", "Ptr", clsidMMDE, "Ptr", 0, "UInt", 0x1, "Ptr", iidMMDE, "Ptr*", &pEnum)
    if (hrEnum != 0 || !pEnum)
        throw Error("CoCreateInstance(MMDeviceEnumerator) hr=" Format("{:08X}", hrEnum))
    ; IMMDeviceEnumerator::GetDefaultAudioEndpoint (vtable idx 4): eRender=0, eConsole=0
    pDevice := 0
    vtbl := NumGet(pEnum, "Ptr")
    hrDev := DllCall(NumGet(vtbl + 4 * A_PtrSize, "Ptr"), "Ptr", pEnum, "UInt", 0, "UInt", 0, "Ptr*", &pDevice)
    if (hrDev != 0 || !pDevice)
        throw Error("GetDefaultAudioEndpoint hr=" Format("{:08X}", hrDev))
    ; IMMDevice::Activate (vtable idx 3): IID_IAudioMeterInformation, CLSCTX_INPROC_SERVER
    pMeter := 0
    vtbl2 := NumGet(pDevice, "Ptr")
    hrAct := DllCall(NumGet(vtbl2 + 3 * A_PtrSize, "Ptr"), "Ptr", pDevice, "Ptr", iidIAM, "UInt", 0x1, "Ptr", 0, "Ptr*", &pMeter)
    if (hrAct != 0 || !pMeter)
        throw Error("Activate(IAudioMeterInformation) hr=" Format("{:08X}", hrAct))
    audioMeter := pMeter
}

GetAudioPeak() {
    global audioMeter
    if !audioMeter
        return 0.0
    peak := 0.0
    try {
        ; IAudioMeterInformation::GetPeakValue (vtable idx 3)
        vtbl := NumGet(audioMeter, "Ptr")
        DllCall(NumGet(vtbl + 3 * A_PtrSize, "Ptr"), "Ptr", audioMeter, "Float*", &peak)
    }
    return peak
}

; Returns {max, min, count} of per-channel peaks.
; Omnidirectional events (flashbang, nearby explosion): min/max ratio → 1.0
; Directional events (footstep, voice, side gunshot): ratio → 0 (one side much louder)
GetChannelPeaks() {
    global audioMeter
    out := { max: 0.0, min: 0.0, count: 0 }
    if !audioMeter
        return out
    count := 0
    try {
        vtbl := NumGet(audioMeter, "Ptr")
        ; IAudioMeterInformation::GetMeteringChannelCount (idx 4)
        DllCall(NumGet(vtbl + 4 * A_PtrSize, "Ptr"), "Ptr", audioMeter, "UInt*", &count)
    }
    if (count <= 0)
        return out
    peaksBuf := Buffer(count * 4, 0)
    try {
        vtbl := NumGet(audioMeter, "Ptr")
        ; IAudioMeterInformation::GetChannelsPeakValues (idx 5)
        DllCall(NumGet(vtbl + 5 * A_PtrSize, "Ptr"), "Ptr", audioMeter, "UInt", count, "Ptr", peaksBuf)
    }
    mx := 0.0
    mn := 1.0
    Loop count {
        p := NumGet(peaksBuf, (A_Index - 1) * 4, "Float")
        if (p > mx)
            mx := p
        if (p < mn)
            mn := p
    }
    out.max := mx
    out.min := mn
    out.count := count
    return out
}

Log(msg) {
    global logFile
    try FileAppend(FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss") "." Format("{:03}", A_TickCount & 0x3FF) " | " msg "`n", logFile)
}
try FileDelete(logFile)
Log("startup | target=" targetExe " toggle=" hkToggle " eyelids=" hkEyelids " threshold=" coverageThreshold "% poll=" checkInterval "ms peakHunt=" peakHuntMs "ms")
Log("audio meter | " (audioMeter ? "OK ptr=" audioMeter " peak≥" audioPeakThreshold " omni≥" audioOmniRatio " window=" audioConfirmMs "ms (REQUIRED for fire)" : "FAIL — audio gate disabled, visual-only fallback"))

enabled        := true
eyelidsClosed  := false
overlayShown   := false
overlay        := 0
flashState     := 0
peakCoverage   := 0
stateStartTick := 0
currentHoldMs  := 0
currentFadeMs  := 0
fadeValue      := 0

overlay := Gui("+AlwaysOnTop +ToolWindow -Caption +E0x8080020 -DPIScale +LastFound")
overlay.BackColor := bkColor
overlay.Show("x0 y0 w" A_ScreenWidth " h" A_ScreenHeight " NoActivate")
WinSetTransparent(0, overlay.Hwnd)
try DllCall("SetWindowPos", "Ptr", overlay.Hwnd, "Ptr", -1, "Int", 0, "Int", 0, "Int", 0, "Int", 0, "UInt", 0x13)
Log("overlay pre-created hwnd=" overlay.Hwnd " size=" A_ScreenWidth "x" A_ScreenHeight)

toggleHotkeyOK  := false
eyelidsHotkeyOK := false
try {
    Hotkey(hkToggle, (*) => ToggleScript())
    toggleHotkeyOK := true
}
try {
    Hotkey(hkEyelids, (*) => ToggleEyelids())
    eyelidsHotkeyOK := true
}
Log("hotkey register | toggle=" (toggleHotkeyOK ? "OK" : "FAIL") " eyelids=" (eyelidsHotkeyOK ? "OK" : "FAIL"))

A_TrayMenu.Delete()
A_TrayMenu.Add("Enable/Disable (" hkToggle ")", (*) => ToggleScript())
A_TrayMenu.Add("Eyelids (" hkEyelids ")",       (*) => ToggleEyelids())
A_TrayMenu.Add()
A_TrayMenu.Add("Edit settings.ini",             (*) => Run('notepad.exe "' iniFile '"'))
A_TrayMenu.Add("Open log",                      (*) => Run('notepad.exe "' logFile '"'))
A_TrayMenu.Add("Reload",                        (*) => Reload())
A_TrayMenu.Add()
A_TrayMenu.Add("Exit",                          (*) => ExitApp())
A_TrayMenu.Default := "Enable/Disable (" hkToggle ")"

TrayTip("FlashBang ready | toggle=" hkToggle " eyelids=" hkEyelids, "FlashBang")
OnExit(SaveSettings)
SetTimer(PollLoop, -1)   ; one-shot: enters the busy-poll loop and never returns

IsWhite(c) {
    global targetColor, tolerance
    return Abs((c & 0xFF) - (targetColor & 0xFF)) <= tolerance
        && Abs(((c >> 8) & 0xFF) - ((targetColor >> 8) & 0xFF)) <= tolerance
        && Abs(((c >> 16) & 0xFF) - ((targetColor >> 16) & 0xFF)) <= tolerance
}

FastPixel(x, y) {
    global hScreenDC
    cr := DllCall("Gdi32\GetPixel", "Ptr", hScreenDC, "Int", x, "Int", y, "UInt")
    ; COLORREF is 0x00BBGGRR, convert to 0xRRGGBB
    return ((cr & 0xFF) << 16) | (cr & 0xFF00) | ((cr >> 16) & 0xFF)
}

; CS2 flashbang characteristics:
;   1. Fills entire screen uniformly (not localized like muzzle flash or sky)
;   2. Nearly pure white — R ≈ G ≈ B (achromatic)
;   3. Ramps to peak in ~3-5 frames
; This kills sky false positives (blue-tinted, fails achromatic test)
; and kills bright wall false positives (only part of screen, fails uniformity)
IsFlashPixel(x, y) {
    global hScreenDC
    cr := DllCall("Gdi32\GetPixel", "Ptr", hScreenDC, "Int", x, "Int", y, "UInt")
    r := cr & 0xFF
    g := (cr >> 8) & 0xFF
    b := (cr >> 16) & 0xFF
    mn := r < g ? (r < b ? r : b) : (g < b ? g : b)
    mx := r > g ? (r > b ? r : b) : (g > b ? g : b)
    ; Audio gate protects against visual false positives → can use lower brightness floor
    return mn >= 150 && (mx - mn) <= 25
}

QuickDetect() {
    cx := A_ScreenWidth // 2
    cy := A_ScreenHeight // 2
    ; Require 6-of-9 coverage: muzzle flash lights ~1-2 points, real flashbang lights all 9
    hits := 0
    if IsFlashPixel(cx, cy)
        hits++
    if IsFlashPixel(cx - 800, cy)
        hits++
    if IsFlashPixel(cx + 800, cy)
        hits++
    if IsFlashPixel(cx, cy - 600)
        hits++
    if IsFlashPixel(cx, cy + 600)
        hits++
    ; Early-exit once we've passed threshold (saves up to 4 GetPixel calls)
    if hits >= 4
        return true
    if IsFlashPixel(cx - 800, cy - 600)
        hits++
    if IsFlashPixel(cx + 800, cy - 600)
        hits++
    if IsFlashPixel(cx - 800, cy + 600)
        hits++
    if IsFlashPixel(cx + 800, cy + 600)
        hits++
    return hits >= 4
}

; Peak-hunt sampler: 9-point grid, returns percentage.
; 9/9 = 100% (direct 0-53° flash), 8/9 = 89% (53-72°), 7/9 = 78% (72-101°),
; 6/9 or less = 67% (glancing 101-180°). Drives the FlashProfile angle tier selection.
SampleCoverage() {
    cx := A_ScreenWidth // 2
    cy := A_ScreenHeight // 2
    hits := 0
    if IsFlashPixel(cx, cy)
        hits++
    if IsFlashPixel(cx - 800, cy)
        hits++
    if IsFlashPixel(cx + 800, cy)
        hits++
    if IsFlashPixel(cx, cy - 600)
        hits++
    if IsFlashPixel(cx, cy + 600)
        hits++
    if IsFlashPixel(cx - 800, cy - 600)
        hits++
    if IsFlashPixel(cx + 800, cy - 600)
        hits++
    if IsFlashPixel(cx - 800, cy + 600)
        hits++
    if IsFlashPixel(cx + 800, cy + 600)
        hits++
    return hits * 100 / 9
}

FlashProfile(pct) {
    ; [holdMs, fadeMs] — CS:GO reference angle table (full blindness + residual blinding)
    ; 0-53°:    1.88s full + 2.99s fade = 4.87s total
    ; 53-72°:   0.45s full + 2.95s fade = 3.40s total
    ; 72-101°:  0.08s full + 1.87s fade = 1.95s total
    ; 101-180°: 0.08s full + 0.87s fade = 0.95s total
    if (pct >= 95)
        return [1880, 2990]
    if (pct >= 85)
        return [450, 2950]
    if (pct >= 75)
        return [80, 1870]
    return [80, 870]
}

PollLoop() {
    global enabled, eyelidsClosed, flashState, targetExe, cs2Active, cs2CheckTick
    global audioMeter, audioConfirmedUntil, audioPeakThreshold, audioOmniRatio, audioConfirmMs
    global audioSustainMs, audioSustainReleaseRatio, audioPeakBangThreshold
    lastLoggedAudio := 0
    lastAudioDiag   := 0
    lastVisualMiss  := 0
    audioMaxSinceDiag := 0.0
    sustainStartTick  := 0        ; 0 = not currently in a sustain streak
    sustainMaxPeak    := 0.0      ; highest peak seen during current streak
    sustainReleaseFloor := audioPeakThreshold * audioSustainReleaseRatio
    ; Visual persistence: require visual to hold for visualStableMs before firing (filters brief flickers)
    visualArmTick     := 0
    visualStableMs    := 40
    loop {
        if (!enabled || flashState != 0) {
            Sleep(5)
            continue
        }
        tick := A_TickCount
        if (tick - cs2CheckTick > 100) {
            cs2Active := WinActive("ahk_exe " targetExe) != 0
            cs2CheckTick := tick
        }
        if !cs2Active {
            Sleep(5)
            continue
        }
        ; AUDIO half
        audioPasses := false
        audioMax    := 0.0
        audioMin    := 0.0
        audioRatio  := 0.0
        audioChannels := 0
        if audioMeter {
            ap := GetChannelPeaks()
            audioMax := ap.max
            audioMin := ap.min
            audioChannels := ap.count
            audioRatio := (ap.max > 0) ? (ap.min / ap.max) : 0
            if (audioMax > audioMaxSinceDiag)
                audioMaxSinceDiag := audioMax
            ; Sustain + peak-bang tracking.
            ;   Real flashbang: bang (peak >= bangThreshold) followed by tinnitus (sustained >= peakThreshold)
            ;   Wall impact / gunfire: either peaks briefly without sustain, OR sustains without ever hitting bang-loud
            ; Streak state machine: above threshold = extend; below release floor = break; between = hold
            if (audioMax >= audioPeakThreshold) {
                if (sustainStartTick = 0) {
                    sustainStartTick := tick
                    sustainMaxPeak := audioMax
                } else if (audioMax > sustainMaxPeak) {
                    sustainMaxPeak := audioMax
                }
            } else if (audioMax < sustainReleaseFloor) {
                if (sustainStartTick != 0 && tick - sustainStartTick > 50) {
                    Log("audio sustain broken after " (tick - sustainStartTick) "ms (maxPeak=" Round(sustainMaxPeak, 3) " droppedTo=" Round(audioMax, 3) ")")
                }
                sustainStartTick := 0
                sustainMaxPeak := 0.0
            }
            ; Pass check runs EVERY tick during an active streak — peak may dip below threshold mid-tinnitus
            if (sustainStartTick != 0 && tick - sustainStartTick >= audioSustainMs && sustainMaxPeak >= audioPeakBangThreshold) {
                audioPasses := true
                audioConfirmedUntil := tick + audioConfirmMs
                if (tick - lastLoggedAudio > 300) {
                    Log("AUDIO pass bangPeak=" Round(sustainMaxPeak, 3) " sustained=" (tick - sustainStartTick) "ms nowPeak=" Round(audioMax, 3) " ch=" audioChannels)
                    lastLoggedAudio := tick
                }
            }
        }
        ; Periodic audio diagnostic — every 2 seconds, report max peak observed
        if (tick - lastAudioDiag > 2000) {
            Log("audio diag maxPeakSince=" Round(audioMaxSinceDiag, 3) " nowMax=" Round(audioMax, 3) " nowMin=" Round(audioMin, 3) " ratio=" Round(audioRatio, 2) " ch=" audioChannels " audioConfirmed=" (tick < audioConfirmedUntil ? "YES" : "no"))
            lastAudioDiag := tick
            audioMaxSinceDiag := 0.0
        }
        ; VISUAL detection — require 4-of-9 to hold for visualStableMs (rejects transient flickers)
        visualPasses := QuickDetect()
        if visualPasses {
            if (visualArmTick = 0)
                visualArmTick := tick
            if (tick - visualArmTick >= visualStableMs) {
                Log("DETECT visual 4-of-9 held " (tick - visualArmTick) "ms")
                StartFlash(0)
                visualArmTick := 0
                continue
            }
        } else {
            visualArmTick := 0
        }
        if eyelidsClosed {
            StartFlash(100)
            continue
        }
        Sleep(-1)
    }
}

StartFlash(initialPct) {
    global flashState, peakCoverage, stateStartTick, overlay, overlayShown, transparency, fadeValue
    ; CRITICAL PATH: flip alpha first, log/timers after
    try DllCall("user32\SetLayeredWindowAttributes", "Ptr", overlay.Hwnd, "UInt", 0, "UChar", transparency, "UInt", 2)
    overlayShown := true
    fadeValue := transparency
    flashState := 1
    peakCoverage := initialPct
    stateStartTick := A_TickCount
    SetTimer(StateMachine, 1)
    try DllCall("SetWindowPos", "Ptr", overlay.Hwnd, "Ptr", -1, "Int", 0, "Int", 0, "Int", 0, "Int", 0, "UInt", 0x13)
    Log("DETECT initial=" Round(initialPct) "% -> peak-hunt")
}

ForceTopmost(hwnd) {
    try DllCall("SetWindowPos", "Ptr", hwnd, "Ptr", -1, "Int", 0, "Int", 0, "Int", 0, "Int", 0, "UInt", 0x13)
}

ShowOverlay() {
    global overlay, overlayShown, transparency, fadeValue
    if overlayShown
        return
    try {
        WinSetTransparent(transparency, overlay.Hwnd)
        overlayShown := true
        fadeValue := transparency
        Log("overlay SHOWN trans=" transparency)
    } catch as e {
        Log("overlay SHOW FAILED: " e.Message)
        return
    }
    ForceTopmost(overlay.Hwnd)
}

HideOverlay() {
    global overlay, overlayShown, flashState
    if overlayShown {
        try WinSetTransparent(0, overlay.Hwnd)
        overlayShown := false
        flashState := 0
        Log("overlay hidden, resuming detection")
        SetTimer(StateMachine, 0)
    }
}

StateMachine() {
    global flashState, peakCoverage, stateStartTick, peakHuntMs, currentHoldMs, currentFadeMs
    global overlay, overlayShown, transparency, fadeValue, eyelidsClosed
    if !overlayShown {
        SetTimer(StateMachine, 0)
        return
    }
    elapsed := A_TickCount - stateStartTick

    if (flashState == 1) {
        pct := SampleCoverage()
        if (pct > peakCoverage)
            peakCoverage := pct
        if (elapsed >= peakHuntMs) {
            ; Peak-hunt never found any white → initial detection was a false positive, abort
            if (peakCoverage = 0) {
                Log("peak=0% after " peakHuntMs "ms -> false positive, aborting")
                HideOverlay()
                return
            }
            profile := FlashProfile(peakCoverage)
            currentHoldMs := profile[1]
            currentFadeMs := profile[2]
            stateStartTick := A_TickCount
            flashState := 2
            Log("peak=" Round(peakCoverage) "% -> hold=" currentHoldMs "ms fade=" currentFadeMs "ms")
        }
        ForceTopmost(overlay.Hwnd)
        return
    }

    if eyelidsClosed {
        fadeValue := transparency
        try WinSetTransparent(transparency, overlay.Hwnd)
        stateStartTick := A_TickCount
        if (flashState == 3)
            flashState := 2
        ForceTopmost(overlay.Hwnd)
        return
    }

    if (flashState == 2) {
        if (elapsed >= currentHoldMs) {
            stateStartTick := A_TickCount
            flashState := 3
            Log("hold done -> fading " currentFadeMs "ms")
        }
        ForceTopmost(overlay.Hwnd)
        return
    }

    if (flashState == 3) {
        if (elapsed >= currentFadeMs) {
            HideOverlay()
            return
        }
        newAlpha := Ceil(transparency * (1 - elapsed / currentFadeMs))
        if (newAlpha < 1)
            newAlpha := 1
        fadeValue := newAlpha
        try WinSetTransparent(newAlpha, overlay.Hwnd)
        ForceTopmost(overlay.Hwnd)
    }
}

ToggleScript(*) {
    global enabled
    enabled := !enabled
    Log("toggle -> " (enabled ? "enabled" : "disabled"))
    if !enabled
        HideOverlay()
    TrayTip(enabled ? "Enabled" : "Disabled", "FlashBang")
}

ToggleEyelids(*) {
    global eyelidsClosed, flashState
    eyelidsClosed := !eyelidsClosed
    Log("eyelids -> " (eyelidsClosed ? "closed" : "open"))
    if (eyelidsClosed && flashState == 0)
        StartFlash(100)
}

SaveSettings(*) {
    global iniFile, hkToggle, hkEyelids, transparency, checkInterval, coverageThreshold, peakHuntMs, targetExe, bkColor
    try {
        IniWrite(hkToggle,          iniFile, "Settings", "HotkeyToggle")
        IniWrite(hkEyelids,         iniFile, "Settings", "HotkeyEyelids")
        IniWrite(transparency,      iniFile, "Settings", "Transparency")
        IniWrite(checkInterval,     iniFile, "Settings", "CheckInterval")
        IniWrite(coverageThreshold, iniFile, "Settings", "CoverageThreshold")
        IniWrite(peakHuntMs,        iniFile, "Settings", "PeakHuntMs")
        IniWrite(targetExe,         iniFile, "Settings", "TargetExe")
        IniWrite(bkColor,           iniFile, "Settings", "BkColor")
    }
}
