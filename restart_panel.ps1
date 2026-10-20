# Startet den Panel-Server neu (z.B. nach einem Update).
#   powershell -ExecutionPolicy Bypass -File .\restart_panel.ps1
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*panel.pyw*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -like "*run panel-*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path $PSScriptRoot "panel\.running.json") -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName "Projekt-Panel Server"
Write-Output "Panel-Server neu gestartet."
