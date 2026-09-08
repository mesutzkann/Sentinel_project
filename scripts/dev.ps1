<#
.SYNOPSIS
    Starts the SentinelAI development stack on Windows.

.DESCRIPTION
    The project is developed on Windows, where there is no make. This covers the handful of
    commands needed day to day; the Makefile mirrors it for Linux and macOS.

.PARAMETER Task
    up        Start PostgreSQL (core profile) and wait until it accepts connections.
    down      Stop containers, keeping the data volume.
    reset     Stop containers and delete the data volume. Everything is re-seeded on next start.
    backend   Run the API on http://localhost:5080.
    frontend  Run the Vite dev server on http://localhost:5173.
    samples   Build and start the five sample microservices.
    build     Build everything and fail on any warning.

.EXAMPLE
    ./scripts/dev.ps1 up
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('up', 'down', 'reset', 'backend', 'frontend', 'samples', 'build')]
    [string]$Task
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Wait-ForPostgres {
    Write-Host 'Waiting for PostgreSQL...' -ForegroundColor Cyan

    foreach ($attempt in 1..30) {
        docker exec sentinel-postgres pg_isready -U sentinel -d sentinel *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'PostgreSQL is ready.' -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 2
    }

    throw 'PostgreSQL did not become ready within 60 seconds. Check: docker compose logs postgres'
}

switch ($Task) {
    'up' {
        docker compose --profile core up -d
        Wait-ForPostgres
    }

    'down' {
        docker compose --profile core --profile samples down
    }

    'reset' {
        # Deletes the data volume, so schemas are recreated from infrastructure/postgres/init
        # and both the backend and the sample services re-run their migrations and seeds.
        docker compose --profile core --profile samples down -v
        Write-Host 'Data volume removed. Run "./scripts/dev.ps1 up" to start clean.' -ForegroundColor Yellow
    }

    'backend' {
        dotnet run --project (Join-Path $root 'backend/src/Sentinel.Api')
    }

    'frontend' {
        Push-Location (Join-Path $root 'frontend')
        try { npm run dev } finally { Pop-Location }
    }

    'samples' {
        # postgres lives in the core profile, and every sample depends on it, so both
        # profiles have to be named or compose rejects the project.
        docker compose --profile core --profile samples up -d --build
    }

    'build' {
        dotnet build (Join-Path $root 'backend/Sentinel.sln') --warnaserror
        dotnet build (Join-Path $root 'sample-services/Sentinel.Samples.sln') --warnaserror

        Push-Location (Join-Path $root 'frontend')
        try { npm run build } finally { Pop-Location }
    }
}
