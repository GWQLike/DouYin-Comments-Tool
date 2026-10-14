param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$browserHelper = $null

try {
    Set-Location -LiteralPath $PSScriptRoot
    $env:UV_CACHE_DIR = Join-Path $PSScriptRoot 'data\uv-cache'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot 'data\playwright-browsers'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $baseUrl = 'http://127.0.0.1:8765'
    $existingApp = $false
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
        $existingApp = ($health.status -eq 'ok' -and $health.app -eq 'douyin-comments')
    }
    catch {
        $existingApp = $false
    }
    if ($existingApp) {
        Write-Host '工具已经运行，正在打开页面。请保留原来的服务窗口。'
        if (-not $NoBrowser) { Start-Process $baseUrl }
        exit 0
    }

    $portInUse = $false
    $portProbe = New-Object System.Net.Sockets.TcpClient
    try {
        $portProbe.Connect('127.0.0.1', 8765)
        $portInUse = $true
    }
    catch {
        $portInUse = $false
    }
    finally {
        $portProbe.Dispose()
    }
    if ($portInUse) {
        throw '端口 8765 已被其他程序占用，无法确认是本工具。请先关闭占用程序，再重试；脚本不会结束其他进程。'
    }

    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        throw '缺少项目 Python 环境。请先双击“初始化环境.cmd”。'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
        throw '缺少前端构建文件。请先双击“初始化环境.cmd”。'
    }

    # Only this short-lived helper runs hidden. The server keeps this console.
    $helperSource = @'
$ErrorActionPreference = 'SilentlyContinue'
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 1 -ErrorAction Stop
        if ($health.status -eq 'ok' -and $health.app -eq 'douyin-comments') {
            Start-Process 'http://127.0.0.1:8765'
            exit 0
        }
    }
    catch { }
    Start-Sleep -Seconds 1
}
exit 1
'@
    if (-not $NoBrowser) {
        $encodedHelper = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($helperSource))
        $browserHelper = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encodedHelper) -WindowStyle Hidden -PassThru
    }

    Write-Host '抖音评论工具正在启动。' -ForegroundColor Cyan
    if (-not $NoBrowser) { Write-Host '页面就绪后会自动打开。' }
    Write-Host "访问地址：$baseUrl"
    Write-Host '请保留此窗口。退出时先在页面停止采集，再在此窗口按 Ctrl+C。'
    Write-Host ''
    & $projectPython -m backend.app
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -ne 0) {
        throw "服务已退出（退出码 $serverExitCode）。请查看上方错误信息。"
    }
}
catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    if ($null -ne $browserHelper -and -not $browserHelper.HasExited) {
        # This process was created by this script; no unrelated process is stopped.
        Stop-Process -Id $browserHelper.Id -ErrorAction SilentlyContinue
    }
}
