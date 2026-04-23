@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='AutoHotkey64.exe'\" | Where-Object { $_.CommandLine -like '*FlashBangColorChanger*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped.
