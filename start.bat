@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "NEED_DEPLOY=0"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import pydantic_core._pydantic_core; import nonebot" >nul 2>nul
    if errorlevel 1 set "NEED_DEPLOY=1"
) else (
    set "NEED_DEPLOY=1"
)

if "%NEED_DEPLOY%"=="1" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: python not found in PATH, and local venv is missing or broken.
        pause
        exit /b 1
    )
    echo [YuKiKo] local venv missing or unhealthy, running deploy helper...
    python scripts\deploy.py --run
) else (
    "%VENV_PY%" main.py
)

set EXIT_CODE=%ERRORLEVEL%
echo.
echo [YuKiKo] process exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
