# Build a redistributable flashdim release.
#
# Output: dist\flashdim\ (the extracted release tree) + flashdim-release.zip.
# Users extract the zip anywhere, run setup.bat, and get a desktop shortcut.
#
# Requires: py launcher + PyInstaller (`py -3.13 -m pip install pyinstaller "setuptools<81"`)
# and flashdim_hot.dll already built (run build_hot.bat if it's missing).

param([switch]$NoZip)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

if (-not (Test-Path -LiteralPath 'flashdim_hot.dll')) {
    Write-Host 'flashdim_hot.dll missing - run build_hot.bat first.' -ForegroundColor Red
    exit 1
}

Write-Host 'Building flashdim.exe (PyInstaller)...'
# Prefer `py` (Windows Python launcher, picks highest installed) and fall
# back to whatever python.exe is on PATH. Caller can override with $env:PY.
$py = if ($env:PY) { $env:PY }
      elseif (Get-Command py      -ErrorAction SilentlyContinue) { 'py' }
      elseif (Get-Command python  -ErrorAction SilentlyContinue) { 'python' }
      else { $null }
if (-not $py) {
    Write-Host 'No Python found. Install Python 3.10+ from https://www.python.org/downloads/' -ForegroundColor Red
    exit 1
}
& $py -m PyInstaller --onedir --noconsole `
    --name flashdim `
    --add-binary 'flashdim_hot.dll;.' `
    --clean --noconfirm gsi_flashdim.py | Out-Null

$dst = Join-Path $here 'dist\flashdim'
if (-not (Test-Path -LiteralPath (Join-Path $dst 'flashdim.exe'))) {
    Write-Host 'PyInstaller did not produce flashdim.exe.' -ForegroundColor Red
    exit 1
}

Write-Host 'Copying installer + cfg + README into release tree...'
foreach ($f in 'setup.bat','setup.ps1','gamestate_integration_flashdim.cfg','README_RELEASE.txt') {
    if (Test-Path -LiteralPath $f) {
        Copy-Item -Force -LiteralPath $f -Destination $dst
    }
}

if (-not $NoZip) {
    $zip = Join-Path $here 'flashdim-release.zip'
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
    Write-Host 'Zipping...'
    Compress-Archive -Path (Join-Path $dst '*') -DestinationPath $zip -CompressionLevel Optimal
    $size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "Release: $zip ($size MB)"
}

Write-Host 'Done.'
