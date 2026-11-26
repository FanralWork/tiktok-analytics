param([switch]$EnableCUDA)
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSCommandPath)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_HOST = '127.0.0.1:11439'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_MODELS = Join-Path $Root '.cache\ollama'
$cfg = Get-Content -LiteralPath (Join-Path $Root 'config\settings.json') -Raw | ConvertFrom-Json

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.14 x64 или venv недоступен' }
}
& $Python -m pip install --only-binary=:all: --disable-pip-version-check -r (Join-Path $Root 'config\requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости Python' }
if ($EnableCUDA) {
    & $Python -m pip install --only-binary=:all: --no-cache-dir --disable-pip-version-check -r (Join-Path $Root 'config\requirements.cuda.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить библиотеки CUDA для Whisper' }
}

$ffmpegRoot = Join-Path $Root '.cache\ffmpeg'
$ffmpeg = Get-ChildItem -LiteralPath $ffmpegRoot -Filter ffprobe.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ffmpeg) {
    New-Item -ItemType Directory -Path $ffmpegRoot -Force | Out-Null
    $archive = Join-Path $Root '.cache\ffmpeg.7z'
    curl.exe -fLsS --max-time 180 -o $archive 'https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-essentials_build.7z'
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось скачать FFmpeg 9.1' }
    if ((Get-FileHash -LiteralPath $archive).Hash -ne '835B9A733445BA7A56A37A26CF7BDE84E2F3CE7DA2A8EC984276BB2ABB1B922C') {
        throw 'Контрольная сумма FFmpeg 9.1 не совпала'
    }
    tar.exe -xf $archive -C $ffmpegRoot
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось распаковать FFmpeg' }
}

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) { throw 'Установите Ollama для Windows с официального сайта и добавьте в PATH' }
try { Invoke-RestMethod 'http://127.0.0.1:11439/api/tags' -TimeoutSec 3 | Out-Null } catch {
    $logRoot = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logRoot 'ollama.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'ollama.stderr.log')
    Start-Sleep -Seconds 3
}
ollama pull $cfg.ollama_model
if ($LASTEXITCODE -ne 0) { throw 'Не удалось получить локальную модель Ollama' }
