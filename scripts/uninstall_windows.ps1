$ErrorActionPreference = "Stop"

$RootDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$PurgeRuntime = $false
$PurgeState = $false
$PurgeEnv = $false
$PurgeData = $false
$RemoveCli = $true
$AssumeYes = $false
$BackupBeforePurge = $true
$BackupDir = Join-Path $RootDir "backups"
$BackupName = "uninstall_backup"

for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = $args[$i]
    switch ($arg) {
        "--purge-runtime" { $PurgeRuntime = $true; continue }
        "--purge-state" { $PurgeState = $true; continue }
        "--purge-env" { $PurgeEnv = $true; continue }
        "--purge-data" { $PurgeData = $true; continue }
        "--purge-all" {
            $PurgeRuntime = $true
            $PurgeState = $true
            $PurgeEnv = $true
            $PurgeData = $true
            continue
        }
        "--backup-dir" {
            $i++
            if ($i -ge $args.Count) { throw "Missing value for --backup-dir" }
            $BackupDir = $args[$i]
            continue
        }
        "--backup-name" {
            $i++
            if ($i -ge $args.Count) { throw "Missing value for --backup-name" }
            $BackupName = $args[$i]
            continue
        }
        "--no-backup" { $BackupBeforePurge = $false; continue }
        "--keep-cli" { $RemoveCli = $false; continue }
        "--yes" { $AssumeYes = $true; continue }
        default { throw "Unknown option for uninstall.bat: $arg" }
    }
}

$DestructivePurge = $PurgeRuntime -or $PurgeState -or $PurgeEnv -or $PurgeData

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" }
function Write-Warn([string]$Message) { Write-Warning $Message }

function New-BackupArchive {
    param(
        [string]$OutputDir,
        [string]$Prefix
    )

    $items = @()
    foreach ($candidate in @(".env", ".env.prod", "config", "plugins\\config", "storage")) {
        $fullPath = Join-Path $RootDir $candidate
        if (Test-Path -LiteralPath $fullPath) {
            $items += $fullPath
        }
    }
    if ($items.Count -eq 0) {
        return $null
    }

    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $archivePath = Join-Path $OutputDir "$Prefix`_$timestamp.zip"
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Compress-Archive -Path $items -DestinationPath $archivePath -Force
    return $archivePath
}

if (-not $AssumeYes) {
    Write-Host "About to uninstall YuKiKo local artifacts from: $RootDir"
    if ($PurgeRuntime) { Write-Host "- purge runtime: .venv, webui/node_modules, webui/dist" }
    if ($PurgeState) { Write-Host "- purge state: caches, sandboxes, coverage artifacts, temp files" }
    if ($PurgeEnv) { Write-Host "- purge env: .env, .env.prod" }
    if ($PurgeData) { Write-Host "- purge data: storage, logs, runtime and generated local data" }
    if ($DestructivePurge -and $BackupBeforePurge) {
        Write-Host "- safety backup: $(Join-Path $BackupDir "$BackupName`_<timestamp>.zip")"
    }
    $confirm = Read-Host "Continue uninstall? [yes/no]"
    if ($confirm.ToLowerInvariant() -ne "yes") {
        Write-Warn "Uninstall cancelled."
        exit 0
    }
}

if ($DestructivePurge -and $BackupBeforePurge) {
    $backupPath = New-BackupArchive -OutputDir $BackupDir -Prefix $BackupName
    if ($backupPath) {
        Write-Info "Created safety backup before uninstall: $backupPath"
    } else {
        Write-Warn "Unable to create pre-uninstall backup, continuing."
    }
}

$targetPatterns = @(
    "*$RootDir*\\main.py*",
    "*$RootDir*\\start.bat*"
)
Get-CimInstance Win32_Process | Where-Object {
    $commandLine = $_.CommandLine
    if (-not $commandLine) { return $false }
    foreach ($pattern in $targetPatterns) {
        if ($commandLine -like $pattern) { return $true }
    }
    return $false
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    } catch {
        Write-Warn "Failed to stop process $($_.ProcessId): $($_.Exception.Message)"
    }
}

if ($RemoveCli) {
    $cliCandidates = @(
        (Join-Path $env:USERPROFILE "AppData\\Local\\Microsoft\\WindowsApps\\yukiko.bat"),
        (Join-Path $RootDir "yukiko.bat")
    )
    foreach ($candidate in $cliCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
        }
    }
}

$runtimePaths = @(
    (Join-Path $RootDir ".venv"),
    (Join-Path $RootDir "webui\\node_modules"),
    (Join-Path $RootDir "webui\\dist")
)
$statePaths = @(
    (Join-Path $RootDir "storage\\cache"),
    (Join-Path $RootDir "storage\\sandbox"),
    (Join-Path $RootDir "__pycache__"),
    (Join-Path $RootDir ".pytest_cache"),
    (Join-Path $RootDir ".mypy_cache"),
    (Join-Path $RootDir ".ruff_cache"),
    (Join-Path $RootDir ".hypothesis"),
    (Join-Path $RootDir ".coverage"),
    (Join-Path $RootDir "coverage.xml"),
    (Join-Path $RootDir "htmlcov"),
    (Join-Path $RootDir "tmp")
)
$envPaths = @(
    (Join-Path $RootDir ".env"),
    (Join-Path $RootDir ".env.prod")
)
$dataPaths = @(
    (Join-Path $RootDir "storage"),
    (Join-Path $RootDir "logs"),
    (Join-Path $RootDir "runtime"),
    (Join-Path $RootDir "tmp")
)

if ($PurgeRuntime) {
    foreach ($path in $runtimePaths) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Info "Runtime artifacts removed."
}

if ($PurgeState) {
    foreach ($path in $statePaths) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Info "Local state/cache artifacts removed."
}

if ($PurgeEnv) {
    foreach ($path in $envPaths) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Info "Environment files removed."
}

if ($PurgeData) {
    foreach ($path in $dataPaths) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Info "Runtime data directories removed."
}

Write-Info "Uninstall flow completed."
