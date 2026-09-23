@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Agent Room - 多 Agent 协作环境
echo ============================================
echo.
echo 启动中... 浏览器请访问 http://127.0.0.1:8787
echo 按 Ctrl+C 停止服务
echo.
node server.js
pause
