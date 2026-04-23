@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0gsi_flashdim.py"
) else (
    start "" python.exe "%~dp0gsi_flashdim.py"
)
