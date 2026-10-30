param(
    [Parameter(Mandatory = $true)][ValidateSet('Start', 'Stop')][string]$Action,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Remote-Process.ps1')
$statePath = Join-Path $PSScriptRoot '.remote-translation-process.json'
$actionName = if ($Action -eq 'Start') { '启动' } else { '关闭' }
try { $Host.UI.RawUI.WindowTitle = "Apple Music 遥控 · $actionName" } catch {}
Write-Host "Apple Music 遥控 · $actionName" -ForegroundColor Magenta

function Invoke-LocalOllama {
    param([string]$Path, $Body = $null, [int]$TimeoutSeconds = 3)
    $request = [Net.HttpWebRequest]::Create($script:modelUrl + $Path)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = $TimeoutSeconds * 1000
    $request.ReadWriteTimeout = $TimeoutSeconds * 1000
    if ($null -ne $Body) {
        $request.Method = 'POST'
        $request.ContentType = 'application/json; charset=utf-8'
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    $response = $request.GetResponse()
    try {
        $reader = [IO.StreamReader]::new($response.GetResponseStream(), [Text.Encoding]::UTF8)
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}

function Find-OllamaExecutable {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    $candidates = @('D:\Ollama\App\ollama.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'))
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw '没有找到 Ollama：请先安装 Ollama 和 hy-mt2-translate 翻译模型。'
}

function Get-LocalOllamaUri {
    param([string]$Address)
    $parsed = $null
    if (-not [Uri]::TryCreate($Address, [UriKind]::Absolute, [ref]$parsed) -or
            $parsed.Scheme -ne 'http' -or $parsed.Host -notin @('localhost', '127.0.0.1', '[::1]', '::1') -or
            $parsed.UserInfo -or $parsed.Query -or $parsed.Fragment -or $parsed.AbsolutePath -ne '/') {
        throw 'HY_MT2_URL 必须是本机 Ollama 的 HTTP 地址，例如 http://127.0.0.1:11434。'
    }
    return $parsed
}

function Stop-OwnedOllama {
    if (-not (Test-Path -LiteralPath $script:statePath)) { return }
    $saved = Get-Content -LiteralPath $script:statePath -Raw | ConvertFrom-Json
    # Configuration may have changed since startup. Ownership is established
    # by the saved PID, creation time, executable and command, not today's URL.
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$saved.pid)"
    if ($owner) {
        $created = $owner.CreationDate.ToUniversalTime().ToString('o')
        if ($created -ne $saved.created -or $owner.ExecutablePath -ne $saved.executable -or
                $owner.CommandLine -notmatch '(?i)\bollama\.exe"?\s+serve\s*$') {
            throw '记录的 Ollama 进程身份已变化，没有停止无关进程。'
        }
        # The runner is a verified child of this launcher's server.
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($owner.ProcessId)" |
            Where-Object { $_.Name -in @('llama-server.exe', 'ollama_llama_server.exe') } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
        Stop-Process -Id $owner.ProcessId -ErrorAction Stop
        Write-Host '已关闭由启动器开启的 Ollama。'
    }
    Remove-Item -LiteralPath $script:statePath -Force
}

# Serialize double clicks and start/stop operations for this project.
$projectPath = Get-RemoteCanonicalPath $PSScriptRoot
$hash = [Security.Cryptography.SHA256]::Create()
try { $suffix = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectPath.ToLowerInvariant()))).Replace('-', '') }
finally { $hash.Dispose() }
$mutex = [Threading.Mutex]::new($false, 'Local\AppleMusicRemoteTranslation-' + $suffix)
$acquired = $false
$startedHere = $false
try {
    try { $acquired = $mutex.WaitOne(180000) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw '另一个启动／关闭操作仍在进行，请稍后再试。' }
    foreach ($setting in @('HY_MT2_URL', 'HY_MT2_MODEL')) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($setting, 'Process'))) {
            $savedValue = [Environment]::GetEnvironmentVariable($setting, 'User')
            if (-not [string]::IsNullOrWhiteSpace($savedValue)) {
                [Environment]::SetEnvironmentVariable($setting, $savedValue, 'Process')
            }
        }
    }
    $modelUrl = if ($env:HY_MT2_URL) { $env:HY_MT2_URL.Trim().TrimEnd('/') } else { 'http://127.0.0.1:11434' }
    $model = if ($env:HY_MT2_MODEL) { $env:HY_MT2_MODEL.Trim() } else { 'hy-mt2-translate' }
    if ($Action -eq 'Stop') {
        & (Join-Path $PSScriptRoot 'Stop-Remote.ps1')
        try {
            $uri = Get-LocalOllamaUri $modelUrl
            $available = $false
            try { $null = Invoke-LocalOllama '/api/tags'; $available = $true } catch {}
            if ($available) {
                $null = Invoke-LocalOllama '/api/generate' @{ model = $model; keep_alive = 0; stream = $false } 30
                Write-Host "翻译模型已卸载，显存已释放：${model}"
            }
        } catch {
            Write-Host "遥控已关闭；翻译模型卸载未完成：$($_.Exception.Message)" -ForegroundColor Yellow
        } finally { Stop-OwnedOllama }
        Write-Host '遥控（含曲库搜索、专辑／艺人页、歌词服务）已关闭，已清理由启动器开启的 Ollama；Apple Music 的播放不受影响。' -ForegroundColor Green
    } else {
        # Playback does not depend on the optional local translation model.
        & (Join-Path $PSScriptRoot 'Start-Remote.ps1')
        $translationReady = $false
        try {
            $uri = Get-LocalOllamaUri $modelUrl
            $tags = $null
            try { $tags = Invoke-LocalOllama '/api/tags' } catch {}
            if ($null -eq $tags) {
                $listener = Get-NetTCPConnection -State Listen -LocalPort $uri.Port -ErrorAction SilentlyContinue
                if ($listener) { throw "端口 $($uri.Port) 被占用，但不是可用的 Ollama 服务。" }
                $ollama = Find-OllamaExecutable
                $previousHost = $env:OLLAMA_HOST
                $previousModels = $env:OLLAMA_MODELS
                try {
                    $env:OLLAMA_HOST = $uri.Authority
                    $savedModels = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
                    if ($savedModels) { $env:OLLAMA_MODELS = $savedModels }
                    $process = Start-Process -FilePath $ollama -ArgumentList 'serve' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'ollama-launch.log') -RedirectStandardError (Join-Path $PSScriptRoot 'ollama-launch-error.log')
                    $startedHere = $true
                } finally { $env:OLLAMA_HOST = $previousHost; $env:OLLAMA_MODELS = $previousModels }
                $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
                @{ pid = $process.Id; executable = $owner.ExecutablePath; created = $owner.CreationDate.ToUniversalTime().ToString('o'); url = $modelUrl } |
                    ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
                for ($attempt = 0; $attempt -lt 40; $attempt++) {
                    try { $tags = Invoke-LocalOllama '/api/tags'; break } catch { Start-Sleep -Milliseconds 250 }
                    $process.Refresh()
                    if ($process.HasExited) { throw 'Ollama 意外退出，请查看 ollama-launch-error.log。' }
                }
                if ($null -eq $tags) { throw 'Ollama 没有按时就绪，请查看 ollama-launch-error.log。' }
                Write-Host '本地翻译服务 Ollama 已在后台启动。'
            }
            $tagName = if ($model.Split('/')[-1] -match ':') { $model } else { $model + ':latest' }
            if ($tagName -notin @($tags.models.name)) { throw "Ollama 中没有安装翻译模型 ${model}。" }
            Write-Host "正在加载本地翻译模型 ${model}…"
            $null = Invoke-LocalOllama '/api/generate' @{ model = $model; stream = $false; keep_alive = '10m'; options = @{ num_ctx = 8192 } } 120
            $translationReady = $true
        } catch {
            Write-Host "遥控已启动；本地翻译暂不可用：$($_.Exception.Message)" -ForegroundColor Yellow
            if ($startedHere) {
                try { Stop-OwnedOllama } catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
            }
        }
        if ($translationReady) { Write-Host '遥控和本地翻译都已就绪。' -ForegroundColor Green }
        if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765/' }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($Action -eq 'Start' -and $startedHere) {
        try { Stop-OwnedOllama } catch { Write-Host $_.Exception.Message -ForegroundColor Red }
    }
    $startHint = '启动失败。请看上面的提示，或查看 server-error.log 和 ollama-launch-error.log。'
    $stopHint = '关闭失败。请看上面的提示；其他程序没有被停止。'
    Write-Host $(if ($Action -eq 'Start') { $startHint } else { $stopHint }) -ForegroundColor Yellow
    exit 1
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
