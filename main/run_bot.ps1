$ErrorActionPreference = "Continue"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$data = Join-Path $project "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
Set-Location $project

while ($true) {
    $started = Get-Date
    & $python -u (Join-Path $project "bot.py") 1>> (Join-Path $data "bot.stdout.log") 2>> (Join-Path $data "bot.stderr.log")
    $code = $LASTEXITCODE
    Add-Content -Path (Join-Path $data "watchdog.log") -Value "$(Get-Date -Format o) bot exited ($code); restarting in 5 seconds"
    # Avoid a tight loop if configuration or code prevents startup.
    Start-Sleep -Seconds 5
}
