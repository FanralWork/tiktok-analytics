param([switch]$RetryErrors)
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSCommandPath)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Logs = Join-Path $Root 'logs'
$PidFile = Join-Path $Logs 'captions.pid'
$env:PYTHONUTF8 = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'

if (Test-Path -LiteralPath $PidFile) {
    $OldPid = [int](Get-Content -LiteralPath $PidFile -Raw)
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        throw "Процесс $OldPid уже зарегистрирован. Проверьте logs\run.log."
    }
}
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Arguments = @('scripts\tiktok_analysis.py', 'process', '--allow-partial-catalog', '--captions-only')
if ($RetryErrors) { $Arguments += '--retry-errors' }
$Process = Start-Process -PassThru -WindowStyle Hidden -WorkingDirectory $Root -FilePath $Python `
    -ArgumentList $Arguments `
    -RedirectStandardOutput (Join-Path $Logs "captions-$Stamp.stdout.log") `
    -RedirectStandardError (Join-Path $Logs "captions-$Stamp.stderr.log")
Set-Content -LiteralPath $PidFile -Value $Process.Id
Write-Output "PID $($Process.Id). Прогресс в logs\run.log."
