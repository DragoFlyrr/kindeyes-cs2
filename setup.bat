@echo off
REM Double-clickable wrapper for setup.ps1.
REM Uses -ExecutionPolicy Bypass so the script runs without PS policy edits.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
