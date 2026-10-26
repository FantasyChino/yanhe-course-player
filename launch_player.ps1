param(
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)][int]$Port = 8767,
    [string]$Config = ''
)

$ErrorActionPreference = 'Stop'
$PlayerProject = [IO.Path]::GetFullPath($PSScriptRoot)
$PlayerUrl = "http://127.0.0.1:$Port/"
$PlayerHash = [Security.Cryptography.SHA256]::Create()
try {
    $PlayerInstance = ([BitConverter]::ToString($PlayerHash.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($PlayerProject.ToLowerInvariant())))).Replace('-', '').ToLowerInvariant().Substring(0, 16)
} finally { $PlayerHash.Dispose() }
$PlayerMutex = $null
$PlayerOwnsMutex = $false

function Get-PlayerHealth {
    try { return Invoke-RestMethod -Uri ($PlayerUrl + 'api/health') -TimeoutSec 2 -ErrorAction Stop }
    catch { return $null }
}

function Find-PlayerPython {
    $PlayerCandidates = @()
    foreach ($PlayerCommandName in @('python.exe', 'py.exe', 'python3.exe', 'pythonw.exe')) {
        $PlayerCommand = Get-Command $PlayerCommandName -ErrorAction SilentlyContinue
        if ($null -ne $PlayerCommand -and $PlayerCommand.Source -notlike '*\WindowsApps\*') {
            $PlayerCandidate = $PlayerCommand.Source
            if ($PlayerCommandName -eq 'pythonw.exe') { $PlayerCandidate = Join-Path (Split-Path $PlayerCandidate) 'python.exe' }
            $PlayerCandidates += $PlayerCandidate
        }
    }
    foreach ($PlayerCandidate in ($PlayerCandidates | Select-Object -Unique)) {
        try {
            $PlayerProbeCode = 'import sys; sys.exit(1) if sys.version_info < (3,10) else None; print(sys.executable)'
            $PlayerProbeArgs = @('-c', $PlayerProbeCode)
            if ([IO.Path]::GetFileName($PlayerCandidate) -ieq 'py.exe') { $PlayerProbeArgs = @('-3') + $PlayerProbeArgs }
            $PlayerResolved = & $PlayerCandidate @PlayerProbeArgs 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $PlayerResolved) { continue }
            $PlayerResolved = ($PlayerResolved | Select-Object -Last 1).Trim()
            if (-not (Test-Path -LiteralPath $PlayerResolved)) { continue }
            $PlayerWindowless = Join-Path (Split-Path $PlayerResolved) 'pythonw.exe'
            if (Test-Path -LiteralPath $PlayerWindowless) { return $PlayerWindowless }
            return $PlayerResolved
        } catch { continue }
    }
    throw '没有找到 Python 3.10 或更新版本。请先安装 Python，并启用 PATH 或 Python Launcher。'
}

try {
    $PlayerMutex = New-Object System.Threading.Mutex($false, ('Local\YanheDuoPlayer-' + $PlayerInstance + '-' + $Port))
    try { $PlayerOwnsMutex = $PlayerMutex.WaitOne(15000) }
    catch [System.Threading.AbandonedMutexException] { $PlayerOwnsMutex = $true }
    if (-not $PlayerOwnsMutex) { throw '播放器正在启动，请稍后重试。' }

    $PlayerHealth = Get-PlayerHealth
    if ($null -ne $PlayerHealth -and ($PlayerHealth.app -ne 'yanhe-duo-player' -or $PlayerHealth.instance -ne $PlayerInstance)) {
        throw "端口 $Port 已被其他程序或另一份播放器占用。可以使用 -Port 指定另一个端口。"
    }
    if ($null -eq $PlayerHealth) {
        $PlayerSocket = New-Object System.Net.Sockets.TcpClient
        try {
            $PlayerSocket.Connect('127.0.0.1', $Port)
            throw "端口 $Port 被其他程序占用。可以使用 -Port 指定另一个端口。"
        } catch [System.Net.Sockets.SocketException] {
            # No service is using this local port.
        } finally { $PlayerSocket.Dispose() }

        $PlayerPython = Find-PlayerPython
        $PlayerServer = Join-Path $PlayerProject 'local_server.py'
        if (-not (Test-Path -LiteralPath $PlayerServer)) { throw '没有找到 local_server.py。' }
        $PlayerArguments = '"' + $PlayerServer + '" --port ' + $Port + ' --no-browser'
        if ($Config) {
            $PlayerConfigPath = [IO.Path]::GetFullPath($Config)
            if (-not (Test-Path -LiteralPath $PlayerConfigPath)) { throw '没有找到指定的课程配置文件。' }
            $PlayerArguments += ' --config "' + $PlayerConfigPath + '"'
        }
        $env:PYTHONIOENCODING = 'utf-8'
        $PlayerProcess = Start-Process -FilePath $PlayerPython -ArgumentList $PlayerArguments `
            -WorkingDirectory $PlayerProject -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $PlayerProject 'local-server.log') `
            -RedirectStandardError (Join-Path $PlayerProject 'local-server-error.log')
        @{ pid = $PlayerProcess.Id; url = $PlayerUrl; instance = $PlayerInstance } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PlayerProject 'local-server-state.json') -Encoding UTF8
        for ($PlayerAttempt = 0; $PlayerAttempt -lt 30; $PlayerAttempt++) {
            Start-Sleep -Milliseconds 200
            $PlayerHealth = Get-PlayerHealth
            if ($null -ne $PlayerHealth) { break }
            if ($PlayerProcess.HasExited) { throw '播放器启动失败，请检查课程配置与 local-server-error.log。' }
        }
    }
    if ($null -eq $PlayerHealth -or $PlayerHealth.app -ne 'yanhe-duo-player' -or $PlayerHealth.instance -ne $PlayerInstance) {
        throw '本地播放器未能启动，请查看 local-server-error.log。'
    }
    if (-not $NoBrowser) { Start-Process -FilePath $PlayerUrl }
} catch {
    $PlayerFailure = $_.Exception.Message
    Add-Content -LiteralPath (Join-Path $PlayerProject 'launcher-error.log') `
        -Value ((Get-Date).ToString('s') + ' ' + $PlayerFailure) -Encoding UTF8
    if ($NoBrowser) { throw }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($PlayerFailure, '同频课程播放器', 'OK', 'Error') | Out-Null
    exit 1
} finally {
    if ($PlayerOwnsMutex) { $PlayerMutex.ReleaseMutex() }
    if ($null -ne $PlayerMutex) { $PlayerMutex.Dispose() }
}
