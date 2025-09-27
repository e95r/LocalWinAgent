# scripts/create_shortcut.ps1
# Создаёт ярлык "LocalWinAgent Chat.lnk" на рабочем столе, который запускает start_agent.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Join-Path $ScriptDir ".."
$StartScript = Join-Path $ScriptDir "start_agent.ps1"

if (!(Test-Path $StartScript)) {
    Write-Error "Не найден scripts\start_agent.ps1"
}

$WshShell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "LocalWinAgent Chat.lnk"

# Запуск через PowerShell с обходом ExecutionPolicy, чтобы не просило руками запускать.
$Target = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Args = "-ExecutionPolicy Bypass -File `"$StartScript`""

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.Arguments = $Args
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,44"
$Shortcut.Save()

Write-Host "Ярлык создан: $ShortcutPath"
