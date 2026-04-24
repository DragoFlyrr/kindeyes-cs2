@echo off
cd /d "%~dp0"
set GSI_RELOAD=
:loop
REM Prefer the built exe (release bundle) when present so users without
REM Python can reuse this launcher. Falls back to source-mode pythonw.exe
REM for dev checkouts, then python.exe if pythonw is missing.
if exist "%~dp0flashdim.exe" (
    "%~dp0flashdim.exe" %GSI_RELOAD%
) else if exist "%~dp0dist\flashdim\flashdim.exe" (
    "%~dp0dist\flashdim\flashdim.exe" %GSI_RELOAD%
) else (
    where pythonw.exe >nul 2>&1
    if %errorlevel%==0 (
        pythonw.exe "%~dp0gsi_flashdim.py" %GSI_RELOAD%
    ) else (
        python.exe "%~dp0gsi_flashdim.py" %GSI_RELOAD%
    )
)
REM Exit code 7 = reload hotkey. Skip self-test on reload so the
REM overlay is ready for the next flash within ~0.5s instead of ~2s.
if %errorlevel%==7 (
    set GSI_RELOAD=--reload
    goto loop
)
