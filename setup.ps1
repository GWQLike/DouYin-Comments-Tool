$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    Set-Location -LiteralPath $PSScriptRoot
    $env:UV_CACHE_DIR = Join-Path $PSScriptRoot 'data\uv-cache'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot 'data\playwright-browsers'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    function Invoke-Checked {
        param([string]$Program, [string[]]$Arguments)
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Program 执行失败（退出码 $LASTEXITCODE）。请检查上方错误后重试。"
        }
    }

    foreach ($program in @('uv.exe', 'node.exe', 'npm.cmd')) {
        if (-not (Get-Command $program -ErrorAction SilentlyContinue)) {
            throw "缺少 $program。请按 README.md 安装运行环境，再重新打开此脚本。"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'uv.lock'))) {
        throw '缺少 uv.lock，请使用完整项目目录。'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\package-lock.json'))) {
        throw '缺少 frontend/package-lock.json，请使用完整项目目录。'
    }

    Write-Host '检查 Python 3.12（不会自动安装 Python）...'
    Invoke-Checked 'uv.exe' @('python', 'find', '--system', '3.12')
    Write-Host '安装已锁定的 Python 项目依赖...'
    Invoke-Checked 'uv.exe' @('sync', '--frozen', '--python', '3.12', '--extra', 'dev')

    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    Write-Host '检查 MediaCrawler 内核及已安装的 Google Chrome...'
    Invoke-Checked $projectPython @('-m', 'backend.check_engine')

    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')
    try {
        Write-Host '安装前端依赖...'
        Invoke-Checked 'npm.cmd' @('ci')
        Write-Host '构建前端...'
        Invoke-Checked 'npm.cmd' @('run', 'build')
    }
    finally {
        Pop-Location
    }

    Write-Host ''
    Write-Host '初始化完成。双击“启动工具.cmd”即可使用。' -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
