param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$localRoot = Join-Path $projectRoot '.local'
$statePath = Join-Path $localRoot 'host-demo-processes.json'
$pgBin = Join-Path $projectRoot '.tools\postgresql-16.15\pgsql\bin'
$pgData = Join-Path $localRoot 'pgdata'
$pgCtl = Join-Path $pgBin 'pg_ctl.exe'
$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$psql = Join-Path $pgBin 'psql.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$node = if ($nodeCommand) { $nodeCommand.Source } else { $null }
$vite = Join-Path $projectRoot 'frontend\node_modules\vite\bin\vite.js'
$apiUrl = 'http://127.0.0.1:8000'
$targetUrl = 'http://127.0.0.1:8001'
$model = 'qwen2.5:3b-instruct'
$records = @()
$startedPostgres = $false

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        return @($saved.processes)
    }
    return @()
}

function Save-State {
    $state = [ordered]@{
        postgresStartedByScript = $startedPostgres
        processes = @($records)
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Test-HttpReady([string]$url, [string]$property, [string]$expected) {
    try {
        $response = Invoke-RestMethod -Uri $url -TimeoutSec 2
        if (-not $property) { return $true }
        return [string]$response.$property -eq $expected
    }
    catch { return $false }
}

function Start-LocalProcess([string]$name, [string]$url, [string]$property, [string]$expected,
    [string]$filePath, [string[]]$arguments, [string]$workingDirectory, [string]$stdout, [string]$stderr) {
    if (Test-HttpReady $url $property $expected) {
        Write-Host "$name is already responding at $url"
        return
    }

    $port = [uri]$url | Select-Object -ExpandProperty Port
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $port is already occupied, but $name did not pass its health check. Refusing to start a duplicate." }

    $process = Start-Process -FilePath $filePath -ArgumentList $arguments -WorkingDirectory $workingDirectory `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $records += [pscustomobject]@{
        name = $name
        processId = $process.Id
        executable = $filePath
        commandMarker = ($arguments -join ' ')
        startedUtc = $process.StartTime.ToUniversalTime().ToString('o')
    }
    Save-State

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-HttpReady $url $property $expected) {
            Write-Host "$name is ready at $url"
            return
        }
        if ($process.HasExited) { throw "$name exited during startup. Inspect $stderr and $stdout." }
    }
    throw "$name did not become healthy within 30 seconds. Inspect $stderr and $stdout."
}

if (-not (Test-Path -LiteralPath $localRoot)) { New-Item -ItemType Directory -Path $localRoot | Out-Null }
$records = @(Read-State)

if ($Action -eq 'status') {
    foreach ($url in @("$apiUrl/api/health", "$targetUrl/health", 'http://127.0.0.1:5173')) {
        try { $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing; "$url : HTTP $([int]$r.StatusCode)" }
        catch { "$url : unavailable" }
    }
    if (Test-Path -LiteralPath $pgIsReady) { & $pgIsReady -h 127.0.0.1 -p 5432 -U policyproof_api -d policyproof }
    exit 0
}

if ($Action -eq 'stop') {
    foreach ($record in @($records)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.processId)" -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $sameStart = $false
        try {
            $currentStart = (Get-Process -Id $record.processId -ErrorAction Stop).StartTime.ToUniversalTime()
            $savedStart = [datetime]::Parse($record.startedUtc).ToUniversalTime()
            $sameStart = [math]::Abs(($currentStart - $savedStart).TotalSeconds) -lt 2
        }
        catch { $sameStart = $false }
        if ($sameStart -and $process.ExecutablePath -eq $record.executable -and
            $process.CommandLine.Contains($record.commandMarker)) {
            Stop-Process -Id $record.processId -Force
            Write-Host "Stopped owned $($record.name) process $($record.processId)"
        }
        else {
            Write-Host "Skipped PID $($record.processId); it no longer matches the recorded $($record.name) process"
        }
    }
    $state = if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } else { $null }
    if ($state -and $state.postgresStartedByScript -and (Test-Path -LiteralPath $pgCtl)) {
        & $pgCtl -D $pgData stop -m fast
    }
    Remove-Item -LiteralPath $statePath -ErrorAction SilentlyContinue
    Write-Host 'Local demo services stopped where this script could verify ownership.'
    exit 0
}

foreach ($required in @($pgCtl, $pgIsReady, $psql, $python, $vite)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required local runtime file is missing: $required" }
}
if (-not $node) { throw 'Node.js is missing from PATH. Install Node.js or add node.exe to PATH.' }
if (-not (Test-Path -LiteralPath $pgData)) { throw "The local PostgreSQL cluster is missing at $pgData. Use the Docker Compose setup or initialize the local fallback first." }

$pgReady = $false
& $pgIsReady -h 127.0.0.1 -p 5432 -U policyproof_api -d policyproof *> $null
if ($LASTEXITCODE -eq 0) { $pgReady = $true }
if (-not $pgReady) {
    & $pgCtl -D $pgData status *> $null
    if ($LASTEXITCODE -eq 0) { throw 'PostgreSQL is running but not accepting connections on 127.0.0.1:5432.' }
    $pgLog = Join-Path $localRoot 'postgres.log'
    & $pgCtl -D $pgData -l $pgLog start -o '-h 127.0.0.1 -p 5432'
    if ($LASTEXITCODE -ne 0) { throw "Could not start local PostgreSQL. Inspect $pgLog." }
    $startedPostgres = $true
    Save-State
}

$env:PGPASSWORD = 'api-dev-only'
& $psql 'postgresql://policyproof_api:api-dev-only@127.0.0.1:5432/policyproof' -Atqc 'SELECT 1' *> $null
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { throw 'The local policyproof database is unavailable. Run the Compose setup or provision infra/init.sql first.' }

try {
    $ollama = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    if (-not (@($ollama.models | ForEach-Object { $_.name }) -contains $model)) {
        throw "Ollama is running, but model $model is not installed. Run: ollama pull $model"
    }
}
catch { throw "Ollama must be running locally with $model available. $($_.Exception.Message)" }

$logDir = Join-Path $localRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$env:DATABASE_URL = 'postgresql://policyproof_target:target-dev-only@127.0.0.1:5432/policyproof'
$env:APP_MODE = 'vulnerable'
Start-LocalProcess 'vulnerable target' "$targetUrl/health" 'status' 'ok' $python `
    @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8001') `
    (Join-Path $projectRoot 'target') (Join-Path $logDir 'target.out.log') (Join-Path $logDir 'target.err.log')

$env:API_DATABASE_URL = 'postgresql://policyproof_api:api-dev-only@127.0.0.1:5432/policyproof'
$env:ORACLE_DATABASE_URL = 'postgresql://policyproof_oracle:oracle-dev-only@127.0.0.1:5432/policyproof'
$env:TARGET_URL = $targetUrl
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = $model
$env:FIXTURE_DIR = Join-Path $projectRoot 'fixtures'
$env:TARGET_SOURCE = Join-Path $projectRoot 'target\app\main.py'
Start-LocalProcess 'backend API' "$apiUrl/api/health" 'status' 'ok' $python `
    @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
    (Join-Path $projectRoot 'backend') (Join-Path $logDir 'backend.out.log') (Join-Path $logDir 'backend.err.log')

$env:VITE_API_BASE = $apiUrl
Start-LocalProcess 'frontend' 'http://127.0.0.1:5173' '' '' $node `
    @($vite, '--host', '127.0.0.1', '--port', '5173', '--strictPort') `
    (Join-Path $projectRoot 'frontend') (Join-Path $logDir 'frontend.out.log') (Join-Path $logDir 'frontend.err.log')

Save-State
Write-Host 'Policy-to-Proof is ready: http://127.0.0.1:5173'
Write-Host "API health: $apiUrl/api/health"
Write-Host 'Use -Action status to inspect services and -Action stop to stop only verified processes recorded by this script.'
