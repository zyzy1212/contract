[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs", "help")]
    [string]$Command = "help"
)

$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$logsDir = Join-Path $root "logs"
$compose = Join-Path $root "docker-compose.yml"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$vite = Join-Path $frontend "node_modules\vite\bin\vite.js"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Test-DockerReady {
    docker info 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Wait-Docker {
    if (Test-DockerReady) { return $true }
    Write-Host "Docker Desktop is not running. Starting it..."
    $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dockerDesktop)) {
        Write-Host "ERROR: Docker Desktop not found. Start it manually."
        return $false
    }
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(150)
    do {
        Start-Sleep -Seconds 5
    } while (-not (Test-DockerReady) -and (Get-Date) -lt $deadline)
    return (Test-DockerReady)
}

function Start-AppProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $out = Join-Path $logsDir "$Name.out.log"
    $err = Join-Path $logsDir "$Name.err.log"
    Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
    Write-Host "started $Name"
}

function Start-Dev {
    if (-not (Test-Path $python)) {
        Write-Host "ERROR: backend venv not found. Run setup first."
        exit 1
    }
    $already = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
    if ($already) {
        Write-Host "ERROR: backend is already listening on :8000. Use 'restart' or run 'stop' first."
        exit 1
    }
    if (-not (Wait-Docker)) {
        Write-Host "ERROR: Docker is required for PostgreSQL."
        exit 1
    }

    Write-Host "==> Starting PostgreSQL"
    docker compose -f $compose up -d postgres 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL failed to start" }
    Write-Host "PostgreSQL started"

    $redisListening = Get-NetTCPConnection -State Listen -LocalPort 6379 -ErrorAction SilentlyContinue
    if (-not $redisListening) {
        Write-Host "==> Redis is not running locally; starting Docker Redis"
        docker compose -f $compose up -d redis 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Redis failed to start" }
    } else {
        Write-Host "==> Redis already running locally"
    }

    Write-Host "==> Applying database migrations"
    Push-Location $backend
    try {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed" }
    } finally {
        Pop-Location
    }

    Start-AppProcess "backend" $python @("-m", "uvicorn", "app.main:app", "--reload", "--port", "8000") $backend
    Start-AppProcess "ingestion" $python @("-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "-Q", "ingestion", "--pool=threads", "--concurrency=2", "--loglevel=INFO", "-n", "ingestion@%h") $backend
    Start-AppProcess "review" $python @("-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "-Q", "review", "--pool=threads", "--concurrency=8", "--loglevel=INFO", "-n", "review@%h") $backend
    if (Test-Path $vite) {
        Start-AppProcess "frontend" "C:\nvm4w\nodejs\node.exe" @($vite) $frontend
    } else {
        Write-Host "WARNING: vite not found; skip frontend"
    }

    Start-Sleep -Seconds 10
    Show-Status
    Write-Host ""
    Write-Host "Frontend: http://localhost:5173"
    Write-Host "Backend:  http://localhost:8000/health"
    Write-Host "Docs:     http://localhost:8000/docs"
}

function Stop-Dev {
    $targets = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq "python.exe" -and $_.CommandLine -match "uvicorn app.main:app|spawn_main") -or
        ($_.Name -eq "python.exe" -and $_.CommandLine -match "celery.*-Q (ingestion|review)") -or
        ($_.Name -eq "node.exe" -and $_.CommandLine -match "vite")
    }
    $targets | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    docker compose -f $compose stop postgres redis 2>&1 | Out-Null
    Write-Host "stopped all dev services"
}

function Show-Status {
    $ports = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in 5432, 6379, 8000, 5173 }
    foreach ($port in 5432, 6379, 8000, 5173) {
        $hit = $ports | Where-Object { $_.LocalPort -eq $port }
        if ($hit) { Write-Host "port $port : listening" } else { Write-Host "port $port : closed" }
    }
}

function Show-Logs {
    Get-ChildItem $logsDir -Filter "*.log" | Sort-Object Name | ForEach-Object {
        Write-Host "=== $($_.Name) ==="
        Get-Content $_.FullName -Tail 30
        Write-Host ""
    }
}

switch ($Command) {
    "start" { Start-Dev }
    "stop" { Stop-Dev }
    "restart" { Stop-Dev; Start-Sleep -Seconds 3; Start-Dev }
    "status" { Show-Status }
    "logs" { Show-Logs }
    default {
        Write-Host @"
Usage: scripts\dev.ps1 <command>

Commands:
  start    Start PostgreSQL, backend, both Celery workers and frontend
  stop     Stop backend, workers, frontend and PostgreSQL
  restart  Stop then start everything
  status   Show which ports are listening
  logs     Show recent logs for all services
"@
    }
}
