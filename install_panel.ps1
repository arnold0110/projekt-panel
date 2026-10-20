# Einmalig ausfuehren (PowerShell, im Ordner docker-autostart):
#   powershell -ExecutionPolicy Bypass -File .\install_panel.ps1
#
# Legt die Desktop-Verknuepfung "Projekt-Panel" an.
# Das Panel braucht nur Python (keine Zusatzpakete) und Microsoft Edge.

$panel = Join-Path $PSScriptRoot "panel\panel.pyw"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) { $pythonw = Join-Path (Split-Path $python) "pythonw.exe" }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Output "pythonw.exe wurde nicht gefunden - ist Python installiert und im PATH?"
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Projekt-Panel.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$panel`""
$lnk.WorkingDirectory = Split-Path $panel
$icon = Join-Path $PSScriptRoot "panel\icon.ico"
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" } else { $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,109" }
$lnk.Description = "Projekt-Panel: Docker-Projekte verwalten"
$lnk.Save()

Write-Output "Verknuepfung angelegt: $lnkPath"
