@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "PYTHON_KIND="
if exist "%ROOT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_KIND=venv"
    goto python_ready
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_KIND=py"
    goto python_ready
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_KIND=python"
    goto python_ready
)

echo [ERROR] Python was not found. Install Python or use the repo .venv.
pause
exit /b 1

:python_ready
if not "%~1"=="" goto run_args

echo.
echo ============================================
echo YuKiKo Cookie Launcher
echo ============================================
echo 1. bilibili
echo 2. douyin
echo 3. kuaishou
echo 4. zhihu
echo 5. weibo
echo 6. all
echo.
set /p SITE_CHOICE=Select site [1-6, default 1]:
if "%SITE_CHOICE%"=="" set "SITE_CHOICE=1"

set "SITE_ARG=--site bilibili"
if "%SITE_CHOICE%"=="2" set "SITE_ARG=--site douyin"
if "%SITE_CHOICE%"=="3" set "SITE_ARG=--site kuaishou"
if "%SITE_CHOICE%"=="4" set "SITE_ARG=--site zhihu"
if "%SITE_CHOICE%"=="5" set "SITE_ARG=--site weibo"
if "%SITE_CHOICE%"=="6" set "SITE_ARG=--all"

echo.
echo Browser:
echo 1. auto
echo 2. edge
echo 3. chrome
echo 4. firefox
set /p BROWSER_CHOICE=Select browser [1-4, default 1]:

set "BROWSER_ARG="
if "%BROWSER_CHOICE%"=="2" set "BROWSER_ARG=--browser edge"
if "%BROWSER_CHOICE%"=="3" set "BROWSER_ARG=--browser chrome"
if "%BROWSER_CHOICE%"=="4" set "BROWSER_ARG=--browser firefox"

set "QR_ARG="
if "%SITE_CHOICE%"=="1" (
    echo.
    set /p USE_QR=Use bilibili QR login? [y/N]:
    if /I "%USE_QR%"=="Y" set "QR_ARG=--qr"
)

echo.
echo Starting cookie extraction...
echo.
call :run_python scripts\get_cookies_windows.py %SITE_ARG% %BROWSER_ARG% %QR_ARG%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Cookie extraction failed with exit code: %EXIT_CODE%
) else (
    echo [OK] Cookie extraction finished.
)
pause
exit /b %EXIT_CODE%

:run_args
call :run_python scripts\get_cookies_windows.py %*
exit /b %ERRORLEVEL%

:run_python
if "%PYTHON_KIND%"=="venv" (
    "%ROOT_DIR%\.venv\Scripts\python.exe" %*
    exit /b %ERRORLEVEL%
)
if "%PYTHON_KIND%"=="py" (
    py -3 %*
    exit /b %ERRORLEVEL%
)
python %*
exit /b %ERRORLEVEL%
