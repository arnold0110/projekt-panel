# Einmalig ausfuehren (PowerShell, im Ordner docker-autostart):
#   powershell -ExecutionPolicy Bypass -File .\register_autostart.ps1
#
# Legt die Aufgabe "Docker-Projekte Autostart" an (startet beim Login)
# und entfernt alte Einzel-Aufgaben, die noch start_kalender.ps1 aufrufen,
# damit der Adventskalender nicht doppelt gestartet wird.

$taskName = "Docker-Projekte Autostart"
$script   = Join-Path $PSScriptRoot "start_all.ps1"

# Alte Adventskalender-Aufgabe(n) finden und entfernen
Get-ScheduledTask | Where-Object {
    $_.Actions | Where-Object { $_.Arguments -like "*start_kalender.ps1*" -or $_.Execute -like "*start_kalender.ps1*" }
} | ForEach-Object {
    $task = $_
    Write-Output "Entferne alte Aufgabe: $($task.TaskName)"
    try {
        Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
    } catch {
        Write-Output "  -> Keine Berechtigung (Aufgabe wurde als Administrator angelegt)."
        Write-Output "     Bitte in einer Admin-PowerShell ausfuehren:"
        Write-Output "     Unregister-ScheduledTask -TaskName '$($task.TaskName)' -Confirm:`$false"
    }
}

$action   = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
            -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"   # 30 s nach dem Login starten
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Startet alle gehosteten Docker-Projekte (deploy.py) nach dem Login." -Force | Out-Null

Write-Output "Aufgabe '$taskName' ist eingerichtet."
Write-Output "Testen ohne Neustart:  Start-ScheduledTask -TaskName '$taskName'"
Write-Output "Log:                   $(Join-Path $PSScriptRoot 'start_all.log')"
