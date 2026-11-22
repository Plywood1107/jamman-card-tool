# Legt eine Verknuepfung "JamMan Card Tool" auf dem Desktop an (22.11.2026).
# Aufruf: verknuepfung-anlegen.ps1 "<Ordner des Werkzeugs>"
param([string]$Ordner = $PSScriptRoot)
if (-not $Ordner) { $Ordner = (Get-Location).Path }
$Ordner = (Resolve-Path $Ordner).Path
$Ziel = Join-Path ([Environment]::GetFolderPath("Desktop")) "JamMan Card Tool.lnk"
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($Ziel)
$s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
$s.Arguments = '"' + (Join-Path $Ordner "starte-jamman.vbs") + '"'
$s.WorkingDirectory = $Ordner
$Symbol = Join-Path $Ordner "jamman.ico"
if (Test-Path $Symbol) { $s.IconLocation = "$Symbol,0" }
$s.Description = "JamMan Card Tool - Speicherkarte verwalten"
$s.WindowStyle = 1
$s.Save()
Write-Host ""
Write-Host "Verknuepfung angelegt:"
Write-Host "  $Ziel"
Write-Host "  Ziel: $($s.TargetPath) $($s.Arguments)"
