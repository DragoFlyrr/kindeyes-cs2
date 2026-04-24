@echo off
cd /d "%~dp0"
set GSI_RELOAD=
:loop
where pythonw.exe >nul 2>&1
if %errorlevel%==0 (
    pythonw.exe "%~dp0gsi_flashdim.py" %GSI_RELOAD%
) else (
    python.exe "%~dp0gsi_flashdim.py" %GSI_RELOAD%
)
REM Exit code 7 = reload hotkey. Skip self-test on reload so the
REM overlay is ready for the next flash within ~0.5s instead of ~2s.
if %errorlevel%==7 (
    set GSI_RELOAD=--reload
    goto loop
)
