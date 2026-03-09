@echo off
chcp 65001 >nul
REM WebUI 快速构建脚本
echo ========================================
echo YuKiKo WebUI 构建工具
echo ========================================
echo.

cd /d "%~dp0webui"

echo [1/2] 检查 node_modules...
if not exist "node_modules" (
    echo node_modules 不存在，正在安装依赖...
    call npm install
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
