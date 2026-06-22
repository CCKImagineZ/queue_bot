# Build a Discloud deploy zip without locked/log files.
# Usage: .\make-discloud-zip.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$zipPath = Join-Path $root "deploy.zip"

$include = @(
    "main.py",
    "queue_cog.py",
    "queue_manager.py",
    "backup_cog.py",
    "requirements.txt",
    "discloud.config",
    "queue_data.json",
    "queues_backup.json",
    ".env"
)

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

$temp = Join-Path $env:TEMP ("discloud-deploy-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null

try {
    foreach ($file in $include) {
        $source = Join-Path $root $file
        if (-not (Test-Path $source)) {
            if ($file -eq ".env") {
                Write-Warning ".env not found - Discloud needs DISCORD_TOKEN in .env"
                continue
            }
            throw "Missing required file: $file"
        }
        Copy-Item $source (Join-Path $temp $file)
    }

    Compress-Archive -Path (Join-Path $temp "*") -DestinationPath $zipPath -Force
    Write-Host "Created: $zipPath"
    Write-Host "Upload this zip via Discloud Commit (not Upload if app already exists)."
}
finally {
    Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
}
