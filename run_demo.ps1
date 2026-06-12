# Web Agent demo runner (Windows PowerShell).
#
# Default: kill any existing gateway, start a fresh one, launch UI on :8090.
# Ctrl+C stops both the UI server and the gateway.
#
# Usage:
#   .\run_demo.ps1              gateway + UI (one command)
#   .\run_demo.ps1 ui           same as default
#   .\run_demo.ps1 tests        pytest only
#   .\run_demo.ps1 hello        run single query (restarts gateway first)
#   .\run_demo.ps1 comparison   5-tool pricing comparison
#   .\run_demo.ps1 wipe         clear state + logs
#   .\run_demo.ps1 all          pytest + canonical queries

param(
    [Parameter(Position = 0)]
    [string]$Command = "ui"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CodeDir = Join-Path $ScriptDir "code"
$GatewayDir = Join-Path $ScriptDir "llm_gatewayV9"
$LogDir = Join-Path $ScriptDir "logs"
$script:GatewayProcess = $null

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Get-QueryText([string]$Id) {
    switch ($Id) {
        "hello" { return "hello" }
        "shannon" { return "When was Claude Shannon born and when did he die? Name three of his contributions to information theory." }
        "populations" { return "Find the populations of London, Paris, Berlin and tell me which two are closest in size." }
        "structured" { return "Compare the populations of Mumbai, Cairo, and Lagos and identify which is growing fastest. Return structured fields per city." }
        "fail" { return "Summarise the contents of /nonexistent/path.txt for me." }
        "browser" { return "What are the top 3 most-liked open-source LLM releases on Hugging Face from the past week? For each give model name, parameter count, and one-line description." }
        "comparison" { return "Compare GitHub Copilot, Cursor, Claude Code, Windsurf, and Tabnine as AI coding tools. For each, open its pricing page, switch the billing toggle if present, and report the free plan, the cheapest paid plan with its price, and three headline features. Give me a single comparison table." }
        default { throw "Unknown query: $Id" }
    }
}

function Test-GatewayOnline {
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:8109/v1/routers" -UseBasicParsing -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

function Sync-Code {
    Push-Location $CodeDir
    try { uv sync --quiet } finally { Pop-Location }
}

function Stop-Gateway {
    try {
        Get-NetTCPConnection -LocalPort 8109 -ErrorAction SilentlyContinue |
            ForEach-Object {
                Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            }
    } catch {
        # Get-NetTCPConnection may require admin on some systems; fall through.
    }

    if ($script:GatewayProcess -and -not $script:GatewayProcess.HasExited) {
        Stop-Process -Id $script:GatewayProcess.Id -Force -ErrorAction SilentlyContinue
    }
    $script:GatewayProcess = $null
    Start-Sleep -Milliseconds 400
}

function Start-GatewayFresh {
    Stop-Gateway
    Write-Host "[webagent] Starting gateway on http://localhost:8109 ..."
    $logOut = Join-Path $LogDir "gateway.log"
    $logErr = Join-Path $LogDir "gateway.err.log"
    $script:GatewayProcess = Start-Process -FilePath "uv" `
        -ArgumentList @("run", "main.py") `
        -WorkingDirectory $GatewayDir `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $logOut `
        -RedirectStandardError $logErr

    for ($i = 0; $i -lt 45; $i++) {
        if (Test-GatewayOnline) {
            Write-Host "[webagent] Gateway ready (pid $($script:GatewayProcess.Id))"
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Gateway failed to start within 45s. See $logOut and $logErr"
}

function Start-WebAgentStack {
    Sync-Code
    Start-GatewayFresh
    Write-Host "[webagent] UI      -> http://127.0.0.1:8090"
    Write-Host "[webagent] Gateway -> http://localhost:8109/"
    Write-Host "[webagent] Press Ctrl+C to stop UI and gateway."
    try {
        Push-Location $CodeDir
        uv run python -m ui.server
    } finally {
        Write-Host "[webagent] Stopping gateway..."
        Stop-Gateway
    }
}

function Run-Pytest {
    Write-Host "===================================================================="
    Write-Host "  Unit tests"
    Write-Host "===================================================================="
    Push-Location $CodeDir
    try { uv run pytest tests/ -v --no-header } finally { Pop-Location }
}

function Run-One([string]$Id) {
    $q = Get-QueryText $Id
    $log = Join-Path $LogDir "$Id.log"
    Write-Host "===================================================================="
    Write-Host "  webagent query: $Id"
    Write-Host "===================================================================="
    Push-Location $CodeDir
    try {
        uv run python flow.py $q 2>&1 | Tee-Object -FilePath $log
    } finally { Pop-Location }
    $sessionsDir = Join-Path $CodeDir "state\sessions"
    if (Test-Path $sessionsDir) {
        $sid = Get-ChildItem $sessionsDir -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty Name
        if ($sid) {
            Push-Location $CodeDir
            try { uv run python comparison_report.py $sid } catch { }
            finally { Pop-Location }
            Write-Host "[webagent] log     -> $log"
            Write-Host "[webagent] session -> $sessionsDir\$sid"
            Write-Host "[webagent] report  -> $sessionsDir\$sid\REPORT.html"
        }
    }
}

switch ($Command) {
    { $_ -in @("", "ui") } {
        Start-WebAgentStack
    }
    "tests" { Sync-Code; Run-Pytest }
    "wipe" {
        Stop-Gateway
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue @(
            (Join-Path $CodeDir "state\sessions"),
            (Join-Path $CodeDir "state\artifacts"),
            (Join-Path $CodeDir "state\index.faiss"),
            (Join-Path $CodeDir "state\index_ids.json"),
            (Join-Path $CodeDir "state\memory.json"),
            $LogDir
        )
        New-Item -ItemType Directory -Path $LogDir | Out-Null
        Write-Host "[webagent] cleared sessions, artifacts, index, memory, logs"
    }
    { $_ -in @("hello", "shannon", "populations", "structured", "fail", "browser", "comparison") } {
        Sync-Code
        Start-GatewayFresh
        try { Run-One $Command } finally { Stop-Gateway }
    }
    "all" {
        Sync-Code
        Start-GatewayFresh
        try {
            Run-Pytest
            foreach ($id in @("hello", "shannon", "populations", "structured", "fail")) {
                Run-One $id
            }
            Write-Host "[webagent] Done. Also try: .\run_demo.ps1 browser | comparison | ui"
        } finally {
            Stop-Gateway
        }
    }
    default { throw "Unknown command: $Command" }
}
