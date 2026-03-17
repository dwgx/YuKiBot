@echo off
chcp 65001 >nul
REM WebUI 快速构建脚本
echo ========================================
echo YuKiKo WebUI 构建工具
echo ========================================
echo.

if defined YUKIKO_NPM_REGISTRY (
    set "NPM_CONFIG_REGISTRY=%YUKIKO_NPM_REGISTRY%"
)
if defined YUKIKO_NPM_CACHE_DIR (
    set "NPM_CONFIG_CACHE=%YUKIKO_NPM_CACHE_DIR%"
    if not exist "%YUKIKO_NPM_CACHE_DIR%" mkdir "%YUKIKO_NPM_CACHE_DIR%"
)
set "NPM_CONFIG_UPDATE_NOTIFIER=false"

cd /d "%~dp0webui"

echo [1/2] 检查 node_modules...
set "NEED_INSTALL=0"
if not exist "node_modules" set "NEED_INSTALL=1"
if "%YUKIKO_WEBUI_FORCE_INSTALL%"=="1" set "NEED_INSTALL=1"
if exist "package-lock.json" if not exist "node_modules\.package-lock.json" set "NEED_INSTALL=1"
if "%NEED_INSTALL%"=="1" (
    echo 正在同步 WebUI 依赖...
    if exist "package-lock.json" (
        call npm ci --prefer-offline --no-audit --no-fund
        if errorlevel 1 call npm install --prefer-offline --no-audit --no-fund
    ) else (
        call npm install --prefer-offline --no-audit --no-fund
    )
    if errorlevel 1 (
        echo 依赖安装失败！
        pause
        exit /b 1
    )
)

echo.
echo [2/2] 构建前端...
call npm run build

if errorlevel 1 (
    echo.
    echo ========================================
    echo 构建失败！
    echo ========================================
    pause
    exit /b 1
) else (
    echo.
    echo ========================================
    echo 构建成功！
    echo ========================================
    echo.
    echo 现在可以刷新浏览器查看更新
    echo WebUI 地址: http://127.0.0.1:8080/webui/config
    echo.
    timeout /t 3
)
