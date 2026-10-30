$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Remote-Process.ps1')
$projectScript = Join-Path $PSScriptRoot 'apple-music-remote.py'
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot 'server.pid') -Force -ErrorAction SilentlyContinue
    Write-Host '遥控服务本来就没有运行。'
    return
}
# Validate all owners before stopping any; IPv4/IPv6 can share one process.
$owners = @($listener | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    Get-CimInstance Win32_Process -Filter "ProcessId = $_"
})
foreach ($owner in $owners) {
    if (-not (Test-RemoteProcess $owner $projectScript)) {
        throw '端口 8765 被其他程序占用，没有停止任何进程。'
    }
}
foreach ($owner in $owners) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($owner.ParentProcessId)"
    Stop-Process -Id $owner.ProcessId -ErrorAction Stop
    if (Test-RemoteProcess $parent $projectScript) {
        Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue
    }
}
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 100
}
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    throw '端口 8765 仍在监听，请检查遥控服务进程。'
}
Remove-Item -LiteralPath (Join-Path $PSScriptRoot 'server.pid') -Force -ErrorAction SilentlyContinue
Write-Host '遥控服务已关闭；Apple Music 的播放不受影响。'
