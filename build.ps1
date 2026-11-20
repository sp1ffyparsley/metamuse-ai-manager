param([string]$Output = (Join-Path $PSScriptRoot 'artifacts'))
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host "[MuseMonitor] Build start: $PSScriptRoot"
& dotnet restore windows/MuseMonitor.csproj --locked-mode
if ($LASTEXITCODE) { throw 'Windows dependency lock restore failed; check installed .NET 10 SDK.' }
& dotnet restore tests/MuseMonitor.Tests.csproj --locked-mode
if ($LASTEXITCODE) { throw 'Test dependency lock restore failed.' }
& dotnet run --project tests/MuseMonitor.Tests.csproj -c Release --no-restore
if ($LASTEXITCODE) { throw 'Client safety tests failed. Do not publish this build.' }
& dotnet publish windows/MuseMonitor.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:NuGetLockFilePath=packages.publish.lock.json -p:RestoreLockedMode=true -o $Output
if ($LASTEXITCODE) { throw 'Windows publish failed; inspect the preceding compiler/restore error.' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install.ps1') -Destination $Output -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $Output -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MANAGER-SETUP.md') -Destination $Output -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD-PARTY-NOTICES.md') -Destination $Output -Force
$revision = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE) { throw 'Source revision could not be determined. Build from a Git checkout.' }
$dirty = [bool](& git status --porcelain)
@{ version='0.3.1-manager-preview.1'; protocol=1; sourceRevision=$revision; dirty=$dirty; systemComplete=$false; builtUtc=[DateTime]::UtcNow.ToString('o'); sha256=(Get-FileHash -LiteralPath (Join-Path $Output 'Muse Monitor.exe')).Hash } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output 'release.json') -Encoding UTF8
Get-FileHash -LiteralPath (Join-Path $Output 'Muse Monitor.exe') -Algorithm SHA256 | Format-List
Write-Host "[MuseMonitor] Complete: $Output"
