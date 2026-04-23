@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe') -and ($_.CommandLine -like '*gsi_flashdim*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped.
