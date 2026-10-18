$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# A newly installed CLI may not yet be reflected in this already-open PowerShell PATH.
$candidateBins = @(
    (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin"),
    "C:\Program Files\Docker\Docker\resources\bin",
    (Join-Path $env:LOCALAPPDATA "Programs\Ollama"),
    "C:\Program Files\Ollama"
)
$availableBins = $candidateBins | Where-Object { Test-Path -LiteralPath $_ }
if ($availableBins) { $env:Path = ($availableBins -join ';') + ";" + $env:Path }

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL 2 is missing. Enable it from an administrator PowerShell, then restart Windows if prompted."
}
$wslInfo = wsl.exe --version 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $wslInfo -match "not installed") {
    throw "WSL 2 is not ready. From an administrator PowerShell, run: wsl --install --no-distribution"
}
$wslStatus = (wsl.exe --status 2>&1 | Out-String) -replace "\x00", ""
if ($wslStatus -match "WSL2 is unable to start since virtualization is not enabled") {
    throw "Windows reports that the WSL2 virtualization platform is not active. Enable the Windows Subsystem for Linux and Virtual Machine Platform features, restart Windows, then rerun this script."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is missing. Install Docker Desktop, then rerun this script."
}
$dockerReady = $false
docker info *> $null
if ($LASTEXITCODE -eq 0) { $dockerReady = $true }
if (-not $dockerReady) {
    $dockerAppCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    )
    $dockerApp = $dockerAppCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($dockerApp) {
        Start-Process -FilePath $dockerApp
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Seconds 2
            docker info *> $null
            if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
        }
    }
}
if (-not $dockerReady) {
    throw "Docker Desktop did not become ready. Check its WSL 2 setup, then rerun this script."
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Ollama CLI is missing. Install Ollama, then rerun this script."
}

$model = "qwen2.5:3b-instruct"
if (Test-Path -LiteralPath (Join-Path $projectRoot ".env")) {
    $modelLine = Get-Content -LiteralPath (Join-Path $projectRoot ".env") |
        Where-Object { $_ -match '^OLLAMA_MODEL=' } |
        Select-Object -First 1
    if ($modelLine) { $model = $modelLine.Substring("OLLAMA_MODEL=".Length).Trim() }
}

$modelList = ollama list 2>$null | Out-String
if ($LASTEXITCODE -ne 0) {
    $ollamaPath = (Get-Command ollama).Source
    Start-Process -FilePath $ollamaPath -ArgumentList "serve" -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 2
        $modelList = ollama list 2>$null | Out-String
        if ($LASTEXITCODE -eq 0) { break }
    }
}
if ($LASTEXITCODE -ne 0) {
    throw "Ollama's local service did not become ready. Inspect the Ollama app, then rerun this script."
}
if ($modelList -notmatch [regex]::Escape($model)) {
    Write-Host "Pulling local model $model ..."
    ollama pull $model
    if ($LASTEXITCODE -ne 0) { throw "Could not pull model $model." }
}

docker compose up --build
exit $LASTEXITCODE
