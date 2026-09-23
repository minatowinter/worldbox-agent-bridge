# 启动 WorldBox 国家情报服务（PowerShell 版）
#
#   .\启动服务.ps1                 默认端口 8777
#   .\启动服务.ps1 -Port 9000      指定端口
#
# 若提示"无法加载文件，因为在此系统上禁止运行脚本"，改用 启动服务.bat，
# 或先执行： Set-ExecutionPolicy -Scope Process RemoteSigned

param(
    [int]$Port = 8777,
    [double]$Interval = 10,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location -Path $PSScriptRoot

$args = @("worldbox_agent.py", "serve", "--port", $Port, "--interval", $Interval)
if ($DataDir -ne "") { $args += @("--data-dir", $DataDir) }

Write-Host ""
Write-Host "  WorldBox 国家情报服务启动中 ..." -ForegroundColor Cyan
Write-Host "  接口地址: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  停止服务: 按 Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

python @args
