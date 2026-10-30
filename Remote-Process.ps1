# Recognize this project through either its real path or a Windows junction.
function Get-RemoteCanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    for ($hop = 0; $hop -lt 16; $hop++) {
        $item = Get-Item -LiteralPath $resolved -Force -ErrorAction Stop
        $redirected = $false
        while ($item) {
            if ($item.LinkType -in @('Junction', 'SymbolicLink') -and $item.Target) {
                $targetPath = @($item.Target)[0]
                if (-not [IO.Path]::IsPathRooted($targetPath)) {
                    $targetPath = Join-Path $item.Parent.FullName $targetPath
                }
                $suffix = $resolved.Substring($item.FullName.Length).TrimStart('\')
                $resolved = if ($suffix) { Join-Path $targetPath $suffix } else { $targetPath }
                $resolved = [IO.Path]::GetFullPath($resolved)
                $redirected = $true
                break
            }
            $item = if ($item -is [IO.FileInfo]) { $item.Directory } else { $item.Parent }
        }
        if (-not $redirected) { return $resolved }
    }
    throw 'Too many filesystem links while resolving the remote project.'
}

function Test-RemoteProcess {
    param($Process, [Parameter(Mandatory = $true)][string]$ProjectScript)
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    $arguments = [regex]::Matches($Process.CommandLine,
        '(?:"([^"\r\n]*apple-music-remote\.py)"|(\S*apple-music-remote\.py))(?=\s|$)',
        [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    foreach ($argument in $arguments) {
        $scriptPath = if ($argument.Groups[1].Success) { $argument.Groups[1].Value } else { $argument.Groups[2].Value }
        if (-not [IO.Path]::IsPathRooted($scriptPath)) { continue }
        try {
            if ([string]::Equals((Get-RemoteCanonicalPath $scriptPath),
                (Get-RemoteCanonicalPath $ProjectScript), [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        } catch {
            continue
        }
    }
    return $false
}
