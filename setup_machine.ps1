# Projekt-Panel: diesen Rechner fuer Fernzugriff + Hosting-Umzug einrichten.
# Einmal pro Rechner ausfuehren (normale PowerShell, im Ordner docker-autostart):
#   powershell -ExecutionPolicy Bypass -File .\setup_machine.ps1
#
# Vorher in Cloudflare Zero Trust erledigen (siehe ANLEITUNG.md):
#   Access-Anwendung fuer die Panel-Subdomains + Service-Token "panel-sync".

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$machineFile = Join-Path $PSScriptRoot "machine.json"

function Ask($text, $default) {
    $suffix = if ($default) { " [$default]" } else { "" }
    $v = Read-Host "$text$suffix"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return $v.Trim()
}

Write-Host ""
Write-Host "=== Projekt-Panel: Rechner einrichten ===" -ForegroundColor Cyan

$old = $null
if (Test-Path $machineFile) {
    $old = Get-Content $machineFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "Vorhandene machine.json gefunden - Werte werden als Vorschlag angeboten."
}

# ---------- Grunddaten
$name = Ask "Name dieses Rechners (z.B. PC oder Laptop)" $(if ($old) { $old.name } else { "PC" })
$slug = ($name.ToLower() -replace '[^a-z0-9-]', '')
$defaultHost = if ($slug -eq "pc") { "panel.arnold0110.com" } else { "panel-$slug.arnold0110.com" }
$hostname = Ask "Panel-Adresse dieses Rechners" $(if ($old) { $old.public_hostname } else { $defaultHost })
$tunnel = "panel-$slug"

# ---------- Cloudflare Tunnel fuer das Panel
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "cloudflared fehlt. Installieren mit: winget install --id Cloudflare.cloudflared" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $env:USERPROFILE ".cloudflared\cert.pem"))) {
    Write-Host "cloudflared ist auf diesem Rechner noch nicht eingeloggt - Browser oeffnet sich ..."
    cloudflared tunnel login
}
$ErrorActionPreference = "Continue"
$list = (cloudflared tunnel list 2>$null | Out-String)
if ($list -notmatch "\s$([regex]::Escape($tunnel))\s") {
    Write-Host "Lege Tunnel '$tunnel' an ..."
    cloudflared tunnel create $tunnel 2>&1 | ForEach-Object { "  $_" } | Out-Host
}
Write-Host "Verknuepfe $hostname mit dem Tunnel ..."
cloudflared tunnel route dns $tunnel $hostname 2>&1 | ForEach-Object { "  $_" } | Out-Host   # stderr als Text, nicht als rote Fehlermeldung
$ErrorActionPreference = "Stop"

# ---------- Cloudflare Access
Write-Host ""
Write-Host "Werte aus Cloudflare Zero Trust (siehe ANLEITUNG.md):" -ForegroundColor Cyan
$team  = Ask "Team-Domain (xyz.cloudflareaccess.com)" $(if ($old) { $old.access.team_domain } else { "" })
$team  = ($team -replace '^https?://', '').TrimEnd('/')
$aud   = Ask "Application Audience (AUD) Tag (optional, Enter = ueberspringen)" $(if ($old) { $old.access.aud } else { "" })
if (-not $aud) { $aud = "" }
$email = Ask "Deine Login-E-Mail" $(if ($old) { $old.access.allowed_emails[0] } else { "arne.com99@googlemail.com" })
$cid   = Ask "Service-Token Client ID" $(if ($old) { $old.service_token.client_id } else { "" })
$csec  = Ask "Service-Token Client Secret" $(if ($old) { $old.service_token.client_secret } else { "" })

# ---------- anderer Rechner
$peers = @()
$peerName = Ask "Name des anderen Rechners (leer lassen, wenn es keinen gibt)" $(if ($old -and $old.peers.Count -gt 0) { $old.peers[0].name } else { $(if ($slug -eq "pc") { "Laptop" } else { "PC" }) })
if ($peerName) {
    $peerSlug = ($peerName.ToLower() -replace '[^a-z0-9-]', '')
    $peerDefault = if ($peerSlug -eq "pc") { "https://panel.arnold0110.com" } else { "https://panel-$peerSlug.arnold0110.com" }
    $peerUrl = Ask "Panel-Adresse von $peerName" $(if ($old -and $old.peers.Count -gt 0) { $old.peers[0].url } else { $peerDefault })
    $peers = @([ordered]@{ name = $peerName; url = $peerUrl })
}

$machine = [ordered]@{
    name            = $name
    port            = 8765
    public_hostname = $hostname
    panel_tunnel    = $tunnel
    access          = [ordered]@{ team_domain = $team; aud = $aud; allowed_emails = @($email) }
    service_token   = [ordered]@{ client_id = $cid; client_secret = $csec }
    peers           = $peers
}
$json = $machine | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($machineFile, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Host "machine.json gespeichert."

# ---------- projects.json (falls neu)
$projectsFile = Join-Path $PSScriptRoot "projects.json"
if (-not (Test-Path $projectsFile)) {
    $p = [ordered]@{ scan_root = $env:USERPROFILE; projects = @() } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($projectsFile, $p, (New-Object System.Text.UTF8Encoding $false))
}

# ---------- Python finden
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) { $pythonw = Join-Path (Split-Path $python) "pythonw.exe" }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe nicht gefunden - ist Python installiert und im PATH?" -ForegroundColor Red
    exit 1
}
$panel = Join-Path $PSScriptRoot "panel\panel.pyw"

# ---------- Aufgabe: Panel-Server beim Login
$taskName = "Projekt-Panel Server"
$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$panel`" --server" -WorkingDirectory (Split-Path $panel)
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Projekt-Panel im Hintergrund (inkl. Panel-Tunnel)" -Force | Out-Null
Write-Host "Aufgabe '$taskName' eingerichtet."

# ---------- Autostart-Aufgabe + Desktop-Verknuepfung
if (-not (Get-ScheduledTask -TaskName "Docker-Projekte Autostart" -ErrorAction SilentlyContinue)) {
    & (Join-Path $PSScriptRoot "register_autostart.ps1")
}
& (Join-Path $PSScriptRoot "install_panel.ps1")

# ---------- laufendes Panel neu starten, damit es die neue Konfiguration nutzt
$running = Join-Path $PSScriptRoot "panel\.running.json"
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*panel.pyw*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item $running -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $taskName

Write-Host ""
Write-Host "Fertig! Panel dieses Rechners: https://$hostname" -ForegroundColor Green
Write-Host "Lokal weiterhin ueber die Desktop-Verknuepfung 'Projekt-Panel'."
