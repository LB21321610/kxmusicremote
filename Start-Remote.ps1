$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Remote-Process.ps1')
$port = 8765
$projectScript = Join-Path $PSScriptRoot 'apple-music-remote.py'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$log = Join-Path $PSScriptRoot 'server.log'
$errorLog = Join-Path $PSScriptRoot 'server-error.log'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'apple_catalog.py') -PathType Leaf)) {
    throw '新版遥控缺少 apple_catalog.py，请恢复曲库模块后再启动。'
}

function Test-RemotePage {
    # A listening port alone can still belong to an old or unhealthy remote.
    # This checks the shipped catalog views without contacting Apple's servers.
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200 -and
            $response.Content.Contains('id=view-alb') -and
            $response.Content.Contains('id=view-art') -and
            $response.Content.Contains('/catalog/search?'))
    } catch { return $false }
}
# The project owns its base Python under .runtime; do not depend on app caches.
$venvConfig = Join-Path $PSScriptRoot '.venv\pyvenv.cfg'
$venvHome = if (Test-Path -LiteralPath $venvConfig) {
    (Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1) -replace '^\s*home\s*=\s*', ''
}
if (-not (Test-Path -LiteralPath $python) -or -not $venvHome -or -not (Test-Path -LiteralPath (Join-Path $venvHome 'python.exe'))) {
    throw "虚拟环境 .venv 的基础 Python 不存在（${venvHome}）。请检查本项目 .runtime\python，并阅读路径说明.md；不要重新指向 Codex 缓存。"
}
# The home Wi-Fi and this PC's LAN address live in network.local.psd1, kept out of Git.
$networkConfig = Join-Path $PSScriptRoot 'network.local.psd1'
if (-not (Test-Path -LiteralPath $networkConfig -PathType Leaf)) {
    throw '缺少 network.local.psd1：请复制 network.example.psd1，填好家里的 Wi-Fi 和本机局域网地址，再以管理员身份运行 Configure-Network.ps1。'
}
$lan = Import-PowerShellDataFile -LiteralPath $networkConfig
$network = Get-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex -ErrorAction Stop
if ($network.Name -ne $lan.WifiName -or $network.NetworkCategory -ne 'Private') {
    throw "请先连接家里的专用 Wi-Fi（$($lan.WifiName)）再启动遥控。"
}
$address = Get-NetIPAddress -InterfaceIndex $lan.InterfaceIndex -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $lan.Address -and $_.PrefixLength -eq $lan.PrefixLength }
if (-not $address) { throw "这台电脑的局域网地址已不是 $($lan.Address)：请先更新 network.local.psd1，再以管理员身份重新运行 Configure-Network.ps1。" }
if (@(Get-NetFirewallProfile | Where-Object { $_.Enabled -ne 'True' }).Count) {
    throw '所有网络类型的 Windows 防火墙都必须保持开启。'
}
$allow = Get-NetFirewallRule -Name 'AppleMusicRemote-8765-PrivateLAN'
$block = Get-NetFirewallRule -Name 'AppleMusicRemote-8765-PublicDomain-Block'
if ($allow.Enabled -ne 'True' -or $allow.Profile -ne 'Private' -or $allow.Action -ne 'Allow' -or $block.Enabled -ne 'True' -or $block.Action -ne 'Block') {
    throw '遥控需要的防火墙规则没有生效：请以管理员身份重新运行 Configure-Network.ps1。'
}
$expectedProgram = Get-RemoteCanonicalPath (Join-Path $venvHome 'python.exe')
foreach ($rule in @($allow, $block)) {
    $program = ($rule | Get-NetFirewallApplicationFilter).Program
    if (-not [string]::Equals($program, $expectedProgram, [StringComparison]::OrdinalIgnoreCase)) {
        throw '防火墙中的 Python 路径与虚拟环境不一致。请以管理员身份运行 Set-RemoteRuntimeFirewall.ps1，再重新启动。'
    }
}
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener[0].OwningProcess)"
    if (-not (Test-RemoteProcess $owner $projectScript)) {
        throw "端口 ${port} 被其他程序占用，没有停止任何进程。"
    }
    # The web page is read once at startup: a server older than any project
    # module would keep serving the previous version, so restart it.
    $newest = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest -and $newest.LastWriteTime -gt $owner.CreationDate) {
        Write-Host "检测到程序已更新（$($newest.Name)），正在重启遥控服务以加载新版本…"
        & (Join-Path $PSScriptRoot 'Stop-Remote.ps1')
        $listener = $null
    } elseif (-not (Test-RemotePage)) {
        Write-Host '当前服务没有就绪的新版曲库页面，正在重新启动…'
        & (Join-Path $PSScriptRoot 'Stop-Remote.ps1')
        $listener = $null
    } else {
        Write-Host '遥控服务已在运行，已是最新版本。'
    }
}
if (-not $listener) {
    # Explorer/older terminals may predate saved user environment changes.
    # Keep explicit process settings; otherwise read the current Windows user values.
    foreach ($setting in @('HY_MT2_URL', 'HY_MT2_MODEL', 'REMOTE_CATALOG_STOREFRONT', 'REMOTE_CATALOG_LANGUAGE')) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($setting, 'Process'))) {
            $savedValue = [Environment]::GetEnvironmentVariable($setting, 'User')
            if (-not [string]::IsNullOrWhiteSpace($savedValue)) {
                [Environment]::SetEnvironmentVariable($setting, $savedValue, 'Process')
            }
        }
    }
    $savedValue = $null
    $env:PYTHONIOENCODING = 'utf-8'
    $process = Start-Process -FilePath $python -ArgumentList @('-u', ('"' + $projectScript + '"'), '--port', "$port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errorLog -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'server.pid')
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-RemotePage) { $ready = $true; break }
        Start-Sleep -Milliseconds 200
        $process.Refresh()
        if ($process.HasExited) { break }
    }
    if (-not $ready) { throw "遥控服务没有成功启动，请查看 ${errorLog}" }
    Write-Host '遥控服务已在后台启动。'
}
Write-Host '新版曲库搜索、专辑／艺人页和动态背景已载入。'
if (-not (Get-Process -Name AppleMusic -ErrorAction SilentlyContinue)) {
    Start-Process explorer.exe -ArgumentList 'shell:AppsFolder\AppleInc.AppleMusicWin_nzyj5cx40ttqa!App' -WindowStyle Hidden
    Write-Host '已打开 Apple Music，请在其中载入歌曲。'
}
Write-Host "Mac／iPhone 打开：http://$($lan.Address):${port}" -ForegroundColor Cyan
Write-Host '请保持 Windows 不睡眠、Apple Music 打开；读取待播队列需要 Apple Music 窗口在当前桌面。'
try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/status" -TimeoutSec 10
    if ($status.ok -and $status.loaded -ne $false -and $status.title) {
        $state = if ($status.playing) { '正在播放' } else { '已暂停' }
        $artist = if ($status.artist) { ' — ' + $status.artist } else { '' }
        Write-Host "Apple Music ${state}：$($status.title)${artist}"
    } else {
        Write-Host 'Apple Music 还没有运行或没有播放过歌曲；打开 Apple Music 播放一首后即可遥控。'
    }
} catch {
    Write-Host '暂时读不到播放状态，稍后刷新网页即可。'
}
