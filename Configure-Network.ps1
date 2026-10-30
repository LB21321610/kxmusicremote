$ErrorActionPreference = 'Stop'
$resultPath = Join-Path $PSScriptRoot 'network-result.json'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Administrator rights are required to configure Windows Firewall.'
    }
    # The home Wi-Fi and this PC's LAN address live in network.local.psd1, kept out of Git.
    $networkConfig = Join-Path $PSScriptRoot 'network.local.psd1'
    if (-not (Test-Path -LiteralPath $networkConfig -PathType Leaf)) {
        throw 'Copy network.example.psd1 to network.local.psd1 and fill in your home network first.'
    }
    $lan = Import-PowerShellDataFile -LiteralPath $networkConfig
    $adapter = Get-NetAdapter -InterfaceIndex $lan.InterfaceIndex
    $profile = Get-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex
    $address = Get-NetIPAddress -InterfaceIndex $lan.InterfaceIndex -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $lan.Address -and $_.PrefixLength -eq $lan.PrefixLength }
    if ($profile.Name -ne $lan.WifiName -or -not $address) {
        throw 'The expected home Wi-Fi network/address has changed. Recheck network settings before applying rules.'
    }
    $backupPath = Join-Path $PSScriptRoot 'network-before.json'
    if (-not (Test-Path -LiteralPath $backupPath)) {
        [ordered]@{
            Connection = $profile | Select-Object Name,InterfaceIndex,InterfaceAlias,NetworkCategory
            Firewall = @(Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction)
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backupPath -Encoding UTF8
    }
    Set-NetFirewallProfile -Profile Private -Enabled True -DefaultInboundAction Block
    Set-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex -NetworkCategory Private
    # Windows venv's python.exe redirects to the underlying interpreter.
    # Firewall application filters must name the process that owns the socket.
    $pythonHome = ((Get-Content -LiteralPath (Join-Path $PSScriptRoot '.venv\pyvenv.cfg') | Where-Object { $_ -match '^home = ' }) -replace '^home = ', '').Trim()
    $program = Join-Path $pythonHome 'python.exe'
    if (-not (Test-Path -LiteralPath $program)) { throw 'The underlying Python interpreter is missing.' }
    $allowName = 'AppleMusicRemote-8765-PrivateLAN'
    $blockName = 'AppleMusicRemote-8765-PublicDomain-Block'
    if (Get-NetFirewallRule -Name $allowName -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $allowName -Enabled True -Direction Inbound -Action Allow -Profile Private -Program $program -Protocol TCP -LocalPort 8765 -LocalAddress $lan.Address -RemoteAddress $lan.Subnet -InterfaceAlias $adapter.Name -EdgeTraversalPolicy Block | Out-Null
    } else {
        New-NetFirewallRule -Name $allowName -DisplayName 'Apple Music Remote - private home LAN only (8765)' -Description "Only $($lan.Subnet) on the private home WLAN may reach this project. No edge traversal." -Enabled True -Direction Inbound -Action Allow -Profile Private -Program $program -Protocol TCP -LocalPort 8765 -LocalAddress $lan.Address -RemoteAddress $lan.Subnet -InterfaceAlias $adapter.Name -EdgeTraversalPolicy Block | Out-Null
    }
    if (Get-NetFirewallRule -Name $blockName -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $blockName -Enabled True -Direction Inbound -Action Block -Profile Public,Domain -Program $program -Protocol TCP -LocalPort 8765 | Out-Null
    } else {
        New-NetFirewallRule -Name $blockName -DisplayName 'Apple Music Remote - block public/domain (8765)' -Enabled True -Direction Inbound -Action Block -Profile Public,Domain -Program $program -Protocol TCP -LocalPort 8765 | Out-Null
    }
    [ordered]@{
        ok = $true
        Time = (Get-Date).ToString('o')
        Connection = Get-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex | Select-Object Name,InterfaceAlias,NetworkCategory
        Firewall = @(Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction)
        Rules = @(Get-NetFirewallRule -Name $allowName,$blockName | Select-Object Name,Enabled,Profile,Direction,Action)
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    @{ok=$false; error=$_.Exception.Message; Time=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
