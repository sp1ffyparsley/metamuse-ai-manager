param([Parameter(Mandatory=$true)][string]$Output,[int]$Seconds=50)
$ErrorActionPreference='Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MuseForegroundProbe {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window,out uint processId);
}
'@
$start=[DateTime]::UtcNow
$samples=New-Object 'System.Collections.Generic.List[object]'
while (([DateTime]::UtcNow-$start).TotalSeconds -lt $Seconds) {
 $window=[MuseForegroundProbe]::GetForegroundWindow();[uint32]$ownerId=0
 [void][MuseForegroundProbe]::GetWindowThreadProcessId($window,[ref]$ownerId)
 $owner=Get-Process -Id $ownerId -ErrorAction SilentlyContinue
 $samples.Add(@{utc=[DateTime]::UtcNow.ToString('o');window=$window.ToInt64();process=$owner.ProcessName})
 Start-Sleep -Milliseconds 100
}
@{startedUtc=$start.ToString('o');endedUtc=[DateTime]::UtcNow.ToString('o');samples=$samples} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Output -Encoding UTF8
