# flashdim installer
#
# Finds CS2 via the Steam registry + libraryfolders.vdf, drops the GSI cfg
# into the right place, creates a desktop shortcut, and optionally sets up
# auto-start on login. Works with either flashdim.exe (release bundle) or
# the source checkout (gsi_flashdim.py + run_gsi.bat).
#
# Run from the extracted folder: right-click setup.bat -> Run, OR
#   powershell -ExecutionPolicy Bypass -File setup.ps1

param(
    [switch]$AutoStart,
    [switch]$NoShortcut,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$cfgName = 'gamestate_integration_flashdim.cfg'
$cfgSrc  = Join-Path $here $cfgName

function Say($msg) { if (-not $Quiet) { Write-Host $msg } }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path -LiteralPath $cfgSrc)) {
    Fail "$cfgName not found next to setup.ps1 (looked in $here). Make sure you extracted the whole zip."
}

function Find-SteamRoot {
    $candidates = @(
        @{ Path = 'HKCU:\Software\Valve\Steam'; Prop = 'SteamPath' },
        @{ Path = 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam'; Prop = 'InstallPath' },
        @{ Path = 'HKLM:\SOFTWARE\Valve\Steam'; Prop = 'InstallPath' }
    )
    foreach ($c in $candidates) {
        try {
            $k = Get-ItemProperty -Path $c.Path -ErrorAction Stop
            $v = $k.$($c.Prop)
            if ($v) {
                $v = $v -replace '/', '\'
                if (Test-Path -LiteralPath $v) { return $v }
            }
        } catch { }
    }
    return $null
}

function Get-SteamLibraries([string]$steamRoot) {
    $libs = New-Object System.Collections.Generic.List[string]
    $libs.Add($steamRoot) | Out-Null
    $vdf = Join-Path $steamRoot 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $vdf) {
        $txt = Get-Content -Raw -LiteralPath $vdf
        foreach ($m in [regex]::Matches($txt, '"path"\s*"([^"]+)"')) {
            $p = $m.Groups[1].Value -replace '\\\\', '\'
            if ($p -and (Test-Path -LiteralPath $p) -and (-not ($libs -contains $p))) {
                $libs.Add($p) | Out-Null
            }
        }
    }
    return $libs
}

function Find-CS2Cfg($libs) {
    foreach ($lib in $libs) {
        $cfgDir = Join-Path $lib 'steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg'
        if (Test-Path -LiteralPath (Join-Path $cfgDir 'boot.vcfg')) { return $cfgDir }
    }
    return $null
}

Say 'flashdim installer'
Say '------------------'
Say 'Finding Steam...'

$steam = Find-SteamRoot
if (-not $steam) {
    Fail 'Could not find Steam in the registry. Install Steam from https://store.steampowered.com/about/ and run this again.'
}
Say "Steam: $steam"

$libs = Get-SteamLibraries $steam
$cfgDir = Find-CS2Cfg $libs
if (-not $cfgDir) {
    Say 'Could not find Counter-Strike 2 in any Steam library:'
    $libs | ForEach-Object { Say "  - $_" }
    Fail 'Install CS2 via Steam, then run this again.'
}
Say "CS2 cfg folder: $cfgDir"

Copy-Item -Force -LiteralPath $cfgSrc -Destination (Join-Path $cfgDir $cfgName)
Say "Installed GSI config."

# ---- pick a launcher: prefer flashdim.exe, fall back to run_gsi.bat + Python
$launcher = $null
$launcherKind = $null
$candidates = @(
    (Join-Path $here 'flashdim.exe'),
    (Join-Path $here 'dist\flashdim\flashdim.exe')
)
foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) { $launcher = $c; $launcherKind = 'exe'; break }
}
if (-not $launcher) {
    $hasPy = (Get-Command pythonw.exe -ErrorAction SilentlyContinue) -or
             (Get-Command python.exe  -ErrorAction SilentlyContinue)
    $bat = Join-Path $here 'run_gsi.bat'
    if ($hasPy -and (Test-Path -LiteralPath $bat)) {
        $launcher = $bat
        $launcherKind = 'bat'
    } else {
        Say ''
        Say 'No runnable flashdim found. Either:'
        Say '  1. Download the flashdim release bundle (contains flashdim.exe) and re-run setup, OR'
        Say '  2. Install Python 3.10+ from https://www.python.org/downloads/ and re-run setup.'
        exit 1
    }
}
Say "Launcher: $launcher ($launcherKind)"

# ---- desktop shortcut
if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desktop 'flashdim.lnk'
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $launcher
    $sc.WorkingDirectory = (Split-Path -Parent $launcher)
    $sc.Description = 'CS2 flashbang dim overlay'
    $sc.Save()
    Say "Desktop shortcut: $lnk"
}

# ---- optional auto-start on login
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$wantAutoStart = $AutoStart
if (-not $PSBoundParameters.ContainsKey('AutoStart') -and -not $Quiet) {
    $resp = Read-Host 'Start flashdim automatically on login? (y/N)'
    $wantAutoStart = ($resp -match '^[Yy]')
}
if ($wantAutoStart) {
    Set-ItemProperty -Path $runKey -Name 'flashdim' -Value "`"$launcher`""
    Say 'Auto-start enabled (HKCU Run key).'
} else {
    # clean up any previous auto-start if user declined
    if (Get-ItemProperty -Path $runKey -Name 'flashdim' -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name 'flashdim'
        Say 'Auto-start removed.'
    }
}

Say ''
Say 'Done. CS2 must run in fullscreen-windowed or borderless (not exclusive fullscreen).'
Say "Launch the overlay:"
Say "  $launcher"
Say ''
Say 'Hotkeys: F8 confirm pulse | F9 toggle | F10 eyelids | F11 reload | F12 kill'

if (-not $Quiet) {
    Write-Host ''
    Write-Host 'Press Enter to close...' -NoNewline
    [void][System.Console]::ReadLine()
}
