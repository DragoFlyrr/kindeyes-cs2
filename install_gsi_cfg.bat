@echo off
setlocal enabledelayedexpansion

set "CFG_NAME=gamestate_integration_flashdim.cfg"
set "SRC=%~dp0%CFG_NAME%"

set "DEST1=%ProgramFiles(x86)%\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"
set "DEST2=%ProgramFiles%\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"
set "DEST3=C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"
set "DEST4=D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"
set "DEST5=E:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"

set "FOUND="

for %%D in ("%DEST1%" "%DEST2%" "%DEST3%" "%DEST4%" "%DEST5%") do (
    if exist "%%~D\boot.vcfg" (
        set "FOUND=%%~D"
        goto :copy
    )
)

echo Could not find CS2 cfg folder in common Steam locations.
echo Please copy "%SRC%" manually into:
echo   ^<SteamLibrary^>\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\
pause
exit /b 1

:copy
echo Copying to: !FOUND!
copy /Y "%SRC%" "!FOUND!\%CFG_NAME%"
if errorlevel 1 (
    echo Copy failed - may need to run as Administrator.
    pause
    exit /b 1
)
echo.
echo Installed. CS2 will auto-load on next launch; no launch option needed.
pause
