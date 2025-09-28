$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "LocalWinAgent.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-ExecutionPolicy Bypass -File `"$projectRoot\scripts\start_agent.ps1`""
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Запуск локального ассистента LocalWinAgent"
$shortcut.IconLocation = "C:\\Windows\\System32\\shell32.dll,42"
$shortcut.Save()

Write-Host "Ярлык создан: $shortcutPath" -ForegroundColor Green
