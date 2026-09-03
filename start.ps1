[CmdletBinding()]
param(
  [switch]$Install = $false,
  [switch]$ShowAppLogs = $false
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

$logs = Join-Path $root "logs"
if (!(Test-Path -LiteralPath $logs -PathType Container)) {
  New-Item -ItemType Directory -Force -Path $logs | Out-Null
}
$launcherLog = Join-Path $logs "launcher.log"

function Write-Info([string]$Text) { Write-Host $Text -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host $Text -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host $Text -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host $Text -ForegroundColor Red }

function Invoke-Captured([string[]]$Arguments) {
  $output = & $script:venvPy @Arguments 2>&1
  $code = $LASTEXITCODE
  if ($output) {
    $output | Out-File -FilePath $script:launcherLog -Append -Encoding utf8
  }
  if ($code -ne 0) {
    throw "Команда Python завершилась с кодом $code. Подробности: $script:launcherLog"
  }
  return $output
}

try {
  $requirements = Join-Path $root "requirements-windows.txt"
  $bootstrapRequirements = Join-Path $root "requirements-bootstrap.txt"
  foreach ($requiredFile in @($requirements, $bootstrapRequirements)) {
    if (!(Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "В папке запуска не найден $([System.IO.Path]::GetFileName($requiredFile))"
    }
  }

  $runtimeDir = Join-Path $root ".runtime-venv"
  $script:venvPy = Join-Path $runtimeDir "Scripts\python.exe"
  $dependencyMarker = Join-Path $runtimeDir ".cryptomathx-dependencies.sha256"

  if ($Install) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    $pythonPrefix = @("-3.10")
    if ($null -eq $pythonCommand) {
      $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
      $pythonPrefix = @()
    }
    if ($null -eq $pythonCommand) {
      throw "Python 3.10 не найден. Установите Python и повторите запуск с -Install."
    }

    $pythonVersion = (& $pythonCommand.Source @pythonPrefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)
    if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.10") {
      throw "Для requirements-windows.txt требуется Python 3.10; найден $($pythonVersion.Trim())"
    }
    Write-Ok "Python: 3.10"

    if (Test-Path -LiteralPath $runtimeDir) {
      Write-Info "Пересоздаю изолированное окружение запуска"
      Remove-Item -LiteralPath $runtimeDir -Recurse -Force
    } else {
      Write-Info "Создаю изолированное окружение запуска"
    }
    & $pythonCommand.Source @pythonPrefix -m venv $runtimeDir 2>&1 |
      Out-File -FilePath $launcherLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
      throw "Не удалось создать изолированное окружение"
    }

    Write-Info "Обновляю pip с проверкой SHA-256"
    Invoke-Captured @(
      "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
      "--require-hashes", "--only-binary=:all:",
      "--index-url=https://pypi.org/simple", "-r", $bootstrapRequirements
    ) | Out-Null

    Write-Info "Устанавливаю зависимости с проверкой SHA-256"
    Invoke-Captured @(
      "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
      "--require-hashes", "--only-binary=:all:",
      "--index-url=https://pypi.org/simple", "-r", $requirements
    ) | Out-Null
    Invoke-Captured @("-m", "pip", "check") | Out-Null
    $bootstrapHash = (Get-FileHash -Algorithm SHA256 $bootstrapRequirements).Hash.ToLowerInvariant()
    $runtimeHash = (Get-FileHash -Algorithm SHA256 $requirements).Hash.ToLowerInvariant()
    $installedFingerprint = "${bootstrapHash}:$runtimeHash"
    Set-Content -LiteralPath $dependencyMarker -Value $installedFingerprint -Encoding UTF8
    Write-Ok "Проверенные зависимости установлены"
  }

  if (!(Test-Path -LiteralPath $script:venvPy -PathType Leaf)) {
    throw "Среда запуска не установлена. Выполните: .\start.ps1 -Install"
  }
  $bootstrapHash = (Get-FileHash -Algorithm SHA256 $bootstrapRequirements).Hash.ToLowerInvariant()
  $runtimeHash = (Get-FileHash -Algorithm SHA256 $requirements).Hash.ToLowerInvariant()
  $expectedFingerprint = "${bootstrapHash}:$runtimeHash"
  $installedFingerprint = if (Test-Path -LiteralPath $dependencyMarker -PathType Leaf) {
    (Get-Content -LiteralPath $dependencyMarker -Raw).Trim()
  } else {
    ""
  }
  if ($installedFingerprint -ne $expectedFingerprint) {
    throw "Зависимости отсутствуют или изменились. Выполните: .\start.ps1 -Install"
  }

  $runtimeVersion = (& $script:venvPy -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)
  if ($LASTEXITCODE -ne 0 -or $runtimeVersion.Trim() -ne "3.10") {
    throw "Окружение запуска повреждено или создано не для Python 3.10"
  }

  $tokenFile = Join-Path $root "BOT_TOKEN.txt"
  if ([string]::IsNullOrWhiteSpace([string]$env:CRYPTOMATHX_BOT_TOKEN) -and
      !(Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
    throw "Не задан токен: установите CRYPTOMATHX_BOT_TOKEN или создайте BOT_TOKEN.txt"
  }

  $env:PYTHONPATH = Join-Path $root "src"
  $env:MPLCONFIGDIR = Join-Path $root ".mplconfig"
  if (!(Test-Path -LiteralPath $env:MPLCONFIGDIR -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $env:MPLCONFIGDIR | Out-Null
  }
  if ($ShowAppLogs -and [string]::IsNullOrWhiteSpace([string]$env:CRYPTOMATHX_LOG_LEVEL)) {
    $env:CRYPTOMATHX_LOG_LEVEL = "DEBUG"
  }

  Write-Info "Проверяю окружение"
  Invoke-Captured @("-m", "pip", "check") | Out-Null
  Invoke-Captured @(
    "-c", "import cryptomathxbot, httpx, matplotlib, telegram; print('READY')"
  ) | Out-Null

  Write-Info "Запускаю CryptoMathXBot"
  & $script:venvPy -m cryptomathxbot
  $exitCode = $LASTEXITCODE
  if ($exitCode -eq 0) {
    Write-Ok "Бот остановлен штатно"
  } else {
    Write-Fail "Бот остановлен с кодом $exitCode"
  }
  exit $exitCode
}
catch {
  Write-Fail $_.Exception.Message
  Write-Warn ("Подробности установки: " + $launcherLog)
  exit 1
}
