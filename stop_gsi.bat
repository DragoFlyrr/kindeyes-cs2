@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe' OR Name='python3.13.exe'\" | Where-Object { $_.CommandLine -like '*gsi_flashdim*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped.
