# scripts/start_agent.ps1
# Запускает LocalWinAgent, если возможно активирует venv, поднимает uvicorn и открывает чат в браузере.

$ErrorActionPreference = "Stop"

# Перейдём в корень проекта (скрипт лежит в scripts/)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Join-Path $ScriptDir ".."
Set-Location $Root

# Активируем venv если есть
$VenvActivate = Join-Path $Root ".venv\Scripts\Activate.ps1"
if (Test-Path $VenvActivate) {
    . $VenvActivate
}

# Проверим, что есть main.py
if (!(Test-Path (Join-Path $Root "main.py"))) {
    Write-Error "Файл main.py не найден."
}

# Адрес
$Addr = "http://127.0.0.1:8765/chat"

# Старт uvicorn (в фоне)
Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList " -m uvicorn main:app --host 127.0.0.1 --port 8765" | Out-Null

# Подождём 1.5 сек и откроем браузер
Start-Sleep -Seconds 2
Start-Process $Addr
Write-Host "LocalWinAgent запущен. Открылся чат: $Addr"
