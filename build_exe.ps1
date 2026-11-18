# Builds the Windows download: dist\clipsys-windows.zip (and the optional GPU add-on).
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'

& $py -m pip install -q pyinstaller
if ($LASTEXITCODE) { throw 'pip install pyinstaller failed' }

# One folder (not one file): a single-file exe would unpack ~400 MB to temp on every start.
# The NVIDIA libraries (1.8 GB) are left out; they ship separately as the GPU add-on.
& $py -m PyInstaller --noconfirm --clean --name clipsys --console --paths src `
    --add-data 'src/clipsys/db/schema.sql;clipsys/db' `
    --collect-all faster_whisper --collect-all ctranslate2 `
    --collect-data googleapiclient --collect-submodules uvicorn `
    --exclude-module nvidia --exclude-module pytest `
    src/clipsys/__main__.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

# Ship the editable files next to the exe. Never the real .env, tokens or database.
$out = 'dist\clipsys'
Copy-Item config $out -Recurse -Force
Copy-Item .env.example, USAGE.md $out -Force
New-Item -ItemType Directory -Force "$out\inbox" | Out-Null
Set-Content "$out\inbox\manifest.csv" 'file,program_id,creator,source_url_or_empty,received_at' -Encoding ascii

# tar.exe (built into Windows) writes zips without Compress-Archive's 2 GB limit.
Remove-Item dist\clipsys-windows.zip, dist\clipsys-gpu-addon.zip -ErrorAction SilentlyContinue
tar.exe -a -cf dist\clipsys-windows.zip -C dist clipsys
if ($LASTEXITCODE) { throw 'zip failed' }

# Optional GPU add-on: unzip into the clipsys folder so the DLLs land in clipsys\gpu\.
$nv = '.venv\Lib\site-packages\nvidia'
if (Test-Path "$nv\cublas\bin") {
    $gpu = 'build\gpu-addon\gpu'
    New-Item -ItemType Directory -Force $gpu | Out-Null
    Copy-Item "$nv\cublas\bin\*.dll", "$nv\cudnn\bin\*.dll" $gpu -Force
    tar.exe -a -cf dist\clipsys-gpu-addon.zip -C build\gpu-addon gpu
    if ($LASTEXITCODE) { throw 'gpu zip failed' }
}

Get-ChildItem dist\*.zip | ForEach-Object { '{0}  {1:n0} MB' -f $_.Name, ($_.Length / 1MB) }
