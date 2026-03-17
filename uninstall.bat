@echo off
setlocal
cd /d "%~dp0"

set "HAS_SCOPE=0"
for %%A in (%*) do (
    if /I "%%~A"=="--purge-runtime" set "HAS_SCOPE=1"
    if /I "%%~A"=="--purge-state" set "HAS_SCOPE=1"
    if /I "%%~A"=="--purge-env" set "HAS_SCOPE=1"
    if /I "%%~A"=="--purge-data" set "HAS_SCOPE=1"
    if /I "%%~A"=="--purge-all" set "HAS_SCOPE=1"
)

set "DEFAULT_SCOPE="
if "%HAS_SCOPE%"=="0" set "DEFAULT_SCOPE=--purge-all"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall_windows.ps1" %DEFAULT_SCOPE% %*
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [YuKiKo] uninstall failed with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
