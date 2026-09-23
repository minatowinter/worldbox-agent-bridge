@echo off
REM 启动 WorldBox 国家情报服务（双击即可）
REM 关掉这个窗口就等于停止服务。
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo.
echo   WorldBox 国家情报服务启动中 ...
echo   接口地址: http://127.0.0.1:8777
echo   停止服务: 直接关闭本窗口，或按 Ctrl+C
echo.
python worldbox_agent.py serve --port 8777 --interval 10 --open-browser
echo.
echo   服务已停止。
pause
