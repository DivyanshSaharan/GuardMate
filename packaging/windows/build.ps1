param([string]$Python = '.venv\Scripts\python.exe', [string]$Version = '0.1.0')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$') { throw 'Invalid release version' }
Push-Location -LiteralPath $repoRoot
try {
    $env:PYTHON_DOTENV_DISABLED = '1'
    Get-ChildItem Env: | Where-Object { $_.Name -match '^(MONGODB_|TINKER_|RENDER|GUARDMATE_ACCESS_)' } |
        ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
    & $Python -c "import sys; assert sys.platform == 'win32' and sys.maxsize > 2**32"
    if ($LASTEXITCODE -ne 0) { throw 'Windows x64 Python is required' }
    & $Python packaging/windows/prepare_assets.py
    if ($LASTEXITCODE -ne 0) { throw 'Public speech asset verification failed' }
    & $Python -m PyInstaller --noconfirm --distpath .cache/windows-build/dist --workpath .cache/windows-build/work packaging/windows/GuardMate.spec
    if ($LASTEXITCODE -ne 0) { throw 'Windows executable build failed' }
    $runtimeRoot = Join-Path $repoRoot '.cache\windows-build\dist\GuardMate'
    $publicCaBundle = Join-Path $runtimeRoot '_internal\certifi\cacert.pem'
    $forbidden = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Force |
        Where-Object {
            $_.Name -match '^\.env(?:\.|$)|^\.data$|^\.cache$|\.sqlite3(?:-|$)|\.pem$|\.pfx$' -and
            $_.FullName -ne $publicCaBundle
        }
    if ($forbidden) { throw 'Private file pattern found in package; refusing release archive' }
    $releaseRoot = Join-Path $repoRoot '.cache\windows-release'
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    $archive = Join-Path $releaseRoot ('GuardMate-runtime-' + $Version + '-win-x64.zip')
    if (Test-Path -LiteralPath $archive) { throw 'Release archive exists; choose a new version' }
    & $Python packaging/windows/create_archive.py --runtime $runtimeRoot --output $archive
    if ($LASTEXITCODE -ne 0) { throw 'Portable runtime archive creation failed' }
    $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    @{ version = $Version; sha256 = $digest; size = (Get-Item -LiteralPath $archive).Length } |
        ConvertTo-Json | Set-Content -LiteralPath ($archive + '.json') -Encoding utf8
    Write-Output ('Common Windows runtime prepared: ' + $archive)
} finally { Pop-Location }
