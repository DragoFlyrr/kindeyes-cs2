@echo off
cd /d "%~dp0"
:loop
where pythonw.exe >nul 2>&1
if %errorlevel%==0 (
    pythonw.exe "%~dp0gsi_flashdim.py"
) else (
    python.exe "%~dp0gsi_flashdim.py"
)
REM Exit code 7 = reload hotkey. Anything else exits the wrapper.
if %errorlevel%==7 (
    timeout /t 1 /nobreak >nul
    goto loop
)
