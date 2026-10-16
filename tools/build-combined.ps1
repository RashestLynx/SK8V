# Builds the whole tree (runtime DLL + ASI) on this PC's toolchain and copies the pair to dist\combined-<commit>.
# Same steps as BUILD.bat, with what this machine needs: the MSVC flavour of the pinned Rust (the default
# rustup host here is GNU) in rust\target-msvc, and Visual Studio 18 2026 (the vs2022 preset's generator
# is not installed).
#   powershell -ExecutionPolicy Bypass -File tools\build-combined.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$env:RUSTUP_TOOLCHAIN = '1.98.1-x86_64-pc-windows-msvc'
$env:CARGO_TARGET_DIR = Join-Path $Root 'rust\target-msvc'
& (Join-Path $PSScriptRoot 'build-rust.ps1')
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'runtime build failed' }

$hostDir = Join-Path $Root 'build\host-combined'
& cmake -S $Root -B $hostDir -G 'Visual Studio 18 2026' -A x64
if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed' }
& cmake --build $hostDir --config Release
if ($LASTEXITCODE -ne 0) { throw 'host build failed' }

$commit = (& git -C $Root rev-parse --short HEAD).Trim()
$dirty = (& git -C $Root status --porcelain) -ne $null
$out = Join-Path $Root ("dist\combined-$commit" + $(if ($dirty) { '-dirty' } else { '' }))
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $hostDir 'host\Release\SkateVLegacy.asi') $out -Force
Copy-Item (Join-Path $Root 'build\runtime\SkateVRuntime.dll') $out -Force
Get-ChildItem $out | ForEach-Object { '{0} {1} {2}' -f $_.Name, $_.Length, (Get-FileHash $_.FullName).Hash.Substring(0, 8) }
Write-Host "combined build -> $out"
