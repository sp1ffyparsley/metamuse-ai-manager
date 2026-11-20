param([string]$Source = $PSScriptRoot, [switch]$Startup)
$ErrorActionPreference = 'Stop'
$destination = Join-Path $env:LOCALAPPDATA 'Programs\MuseMonitor'
$previous = Join-Path $env:LOCALAPPDATA 'Programs\MuseMonitor.previous'
$exe = Join-Path $Source 'Muse Monitor.exe'
if (!(Test-Path -LiteralPath $exe)) { throw "Muse Monitor.exe missing at $Source. Extract the complete Windows release first." }
$running = Get-Process -Name 'Muse Monitor' -ErrorAction SilentlyContinue
if ($running) { throw 'Exit Muse Monitor from its tray menu before updating. VM work can remain running.' }
Write-Host "[MuseMonitor] Installing $exe to $destination"
if (Test-Path -LiteralPath $destination) {
    New-Item -ItemType Directory -Path $previous -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $destination 'Muse Monitor.exe') -Destination $previous -Force
    Write-Host "[MuseMonitor] Previous executable retained: $previous"
}
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath $exe -Destination $destination -Force
if ((Get-FileHash -LiteralPath $exe).Hash -ne (Get-FileHash -LiteralPath (Join-Path $destination 'Muse Monitor.exe')).Hash) { throw 'Installed executable hash mismatch; restore MuseMonitor.previous.' }
$shell = New-Object -ComObject WScript.Shell
$programs = [Environment]::GetFolderPath('Programs')
$link = $shell.CreateShortcut((Join-Path $programs 'Muse Monitor.lnk'))
$link.TargetPath = Join-Path $destination 'Muse Monitor.exe'; $link.WorkingDirectory = $destination; $link.Save()
if ($Startup) {
    $start = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Muse Monitor.lnk'))
    $start.TargetPath=$link.TargetPath; $start.WorkingDirectory=$destination; $start.Save()
    Write-Host '[MuseMonitor] Login startup enabled by installer option.'
}
Write-Host "[MuseMonitor] Installed and verified. Open Start > Muse Monitor. Pairing and local settings remain separate."
