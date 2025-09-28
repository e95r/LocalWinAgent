param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "Запуск LocalWinAgent..." -ForegroundColor Cyan

$python = "python"
$arguments = @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8765")

$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8765/chat"
}

Write-Host "Агент запущен. Нажмите Ctrl+C для остановки." -ForegroundColor Green

try {
    Wait-Process -Id $process.Id
}
finally {
    if (-not $process.HasExited) {
        $process.CloseMainWindow() | Out-Null
        Start-Sleep -Seconds 1
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
    }
    Write-Host "LocalWinAgent остановлен." -ForegroundColor Yellow
}
