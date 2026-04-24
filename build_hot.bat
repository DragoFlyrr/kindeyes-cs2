@echo off
REM Build flashdim_hot.dll from flashdim_hot.c using MSVC.
REM Requires Visual Studio 2022 Community (or Build Tools) installed.

setlocal
set "VCVARS=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VCVARS%" (
    echo ERROR: vcvars64.bat not found at %VCVARS%
    echo Install Visual Studio 2022 Community or adjust path.
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 (
    echo ERROR: vcvars64 failed
    exit /b 1
)

cd /d "%~dp0"
cl /nologo /O2 /LD /MD /W3 flashdim_hot.c /link /OUT:flashdim_hot.dll
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

echo.
echo Built flashdim_hot.dll
dir flashdim_hot.dll | findstr flashdim_hot.dll
endlocal
