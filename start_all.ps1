# Startet beim Windows-Login alle Projekte, bei denen im Projekt-Panel
# "Automatisch starten" eingeschaltet ist (gespeichert in projects.json).
# Wartet erst, bis Docker Desktop bereit ist, und fuehrt dann in jedem
# dieser Projektordner "python deploy.py" aus (in der Panel-Reihenfolge).

Set-Location -Path $PSScriptRoot
# Python-Ausgaben (Umlaute/Emojis) korrekt als UTF-8 lesen
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$logFile = Join-Path $PSScriptRoot "start_all.log"
function Log($text) { "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text" | Out-File -FilePath $logFile -Append -Encoding utf8 }

# Log klein halten (letzte ~2000 Zeilen)
if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 500KB)) {
    Get-Content $logFile -Tail 2000 | Set-Content $logFile -Encoding utf8
}

Log "===== Autostart beginnt ====="

$configFile = Join-Path $PSScriptRoot "projects.json"
if (-not (Test-Path $configFile)) {
    Log "projects.json fehlt - nichts zu tun."
    exit 0
}
$config = Get-Content $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$projects = @($config.projects | Where-Object { $_.autostart -eq $true } | ForEach-Object { $_.path })

if ($projects.Count -eq 0) {
    Log "Kein Projekt hat Autostart eingeschaltet."
    exit 0
}
Log ("Autostart fuer: " + (($projects | ForEach-Object { Split-Path $_ -Leaf }) -join ", "))

# Auf Docker warten (nach einem Neustart braucht Docker Desktop etwas)
$maxWaitSeconds = 300
$waited = 0
while ($waited -lt $maxWaitSeconds) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
    $waited += 5
}
if ($waited -ge $maxWaitSeconds) {
    Log "Docker war nach $maxWaitSeconds Sekunden nicht bereit. Abbruch."
    exit 1
}
Log "Docker bereit nach $waited Sekunden."

foreach ($project in $projects) {
    $name = Split-Path $project -Leaf
    if (-not (Test-Path (Join-Path $project "deploy.py"))) {
        Log "[$name] uebersprungen: kein deploy.py gefunden."
        continue
    }
    # Laeuft das Projekt schon auf einem anderen Rechner? Dann hier nicht starten.
    $guard = python (Join-Path $PSScriptRoot "panel\guard.py") $project 2>&1 | Out-String
    if ($LASTEXITCODE -eq 3) {
        Log "[$name] uebersprungen: $($guard.Trim())"
        continue
    }
    Log "[$name] starte deploy.py ..."
    Push-Location $project
    try {
        # Nicht "*>>" verwenden: PowerShell 5 schreibt damit UTF-16 ins Log
        python deploy.py 2>&1 | ForEach-Object { "    $_" } | Out-File -FilePath $logFile -Append -Encoding utf8
        Log "[$name] fertig (Exit-Code $LASTEXITCODE)."
    } catch {
        Log "[$name] Fehler: $_"
    } finally {
        Pop-Location
    }
}

Log "===== Autostart beendet ====="
