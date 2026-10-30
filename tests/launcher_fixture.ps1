# Isolated launcher shell. Its process functions never affect real processes.
param($Action, $Url, $Owner)
$env:HY_MT2_URL = $Url
$env:HY_MT2_MODEL = 'test-model'
$script:fakeCreated = [DateTime]::Parse('2026-10-01T00:00:00Z').ToUniversalTime()
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter -like 'ProcessId = *') {
        [pscustomobject]@{ProcessId=12345; CreationDate=[DateTime]::Parse('2026-10-01T00:00:00Z').ToUniversalTime(); ExecutablePath='C:\Fake\ollama.exe'; CommandLine='"C:\Fake\ollama.exe" serve'}
    }
}
function Stop-Process {
    param($Id, $ErrorAction)
    $Id | Add-Content -LiteralPath (Join-Path $PSScriptRoot 'killed.txt')
}
if ($Owner -ne 'none') {
    @{pid=12345; executable='C:\Fake\ollama.exe'; created=$(if($Owner -eq 'stale'){'wrong'}else{$script:fakeCreated.ToString('o')}); url='http://127.0.0.1:11434'} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot '.remote-translation-process.json')
}
& (Join-Path $PSScriptRoot 'Remote-With-Translation.ps1') -Action $Action -NoBrowser
if ($LASTEXITCODE) { exit $LASTEXITCODE }
