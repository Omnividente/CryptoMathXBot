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
  $candidateDir = Join-Path $root ".runtime-venv.new"
  $previousDir = Join-Path $root ".runtime-venv.previous"
  $dependencyMarkerName = ".cryptomathx-dependencies.sha256"
  $script:venvPy = Join-Path $runtimeDir "Scripts\python.exe"
  $dependencyMarker = Join-Path $runtimeDir $dependencyMarkerName

  if (!(Test-Path -LiteralPath $runtimeDir) -and
      (Test-Path -LiteralPath $previousDir -PathType Container)) {
    Write-Warn "Восстанавливаю предыдущее окружение после незавершённого обновления"
    Move-Item -LiteralPath $previousDir -Destination $runtimeDir
  }

  if ($Install) {
    $pythonExecutable = $null
    $pythonPrefix = @()
    if (![string]::IsNullOrWhiteSpace([string]$env:CRYPTOMATHX_PYTHON)) {
      if (!(Test-Path -LiteralPath $env:CRYPTOMATHX_PYTHON -PathType Leaf)) {
        throw "CRYPTOMATHX_PYTHON указывает на отсутствующий файл"
      }
      $pythonExecutable = (Resolve-Path -LiteralPath $env:CRYPTOMATHX_PYTHON).Path
    } else {
      $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
      if ($null -ne $pythonLauncher) {
        $candidateVersion = (& $pythonLauncher.Source -3.14 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null)
        if ($LASTEXITCODE -eq 0 -and ([string]$candidateVersion).Trim() -eq "3.14") {
          $pythonExecutable = $pythonLauncher.Source
          $pythonPrefix = @("-3.14")
        }
      }
      if ($null -eq $pythonExecutable) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
          $pythonExecutable = $pythonCommand.Source
        }
      }
    }
    if ($null -eq $pythonExecutable) {
      throw "Python 3.14 не найден. Установите Python и повторите запуск с -Install."
    }

    $pythonVersion = (& $pythonExecutable @pythonPrefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)
    if ($LASTEXITCODE -ne 0 -or ([string]$pythonVersion).Trim() -ne "3.14") {
      throw "Для requirements-windows.txt требуется Python 3.14; найден $(([string]$pythonVersion).Trim())"
    }
    Write-Ok "Python: 3.14"

    if (Test-Path -LiteralPath $candidateDir) {
      Remove-Item -LiteralPath $candidateDir -Recurse -Force
    }
    try {
      Write-Info "Собираю новое изолированное окружение"
      & $pythonExecutable @pythonPrefix -m venv $candidateDir 2>&1 |
        Out-File -FilePath $launcherLog -Append -Encoding utf8
      if ($LASTEXITCODE -ne 0) {
        throw "Не удалось создать новое изолированное окружение"
      }

      $candidatePython = Join-Path $candidateDir "Scripts\python.exe"
      $script:venvPy = $candidatePython
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

      $previousPythonPath = $env:PYTHONPATH
      $env:PYTHONPATH = Join-Path $root "src"
      try {
        Invoke-Captured @(
          "-c", "import cryptomathxbot, httpx, matplotlib, telegram; print('READY')"
        ) | Out-Null
      }
      finally {
        $env:PYTHONPATH = $previousPythonPath
      }

      $bootstrapHash = (Get-FileHash -Algorithm SHA256 $bootstrapRequirements).Hash.ToLowerInvariant()
      $runtimeHash = (Get-FileHash -Algorithm SHA256 $requirements).Hash.ToLowerInvariant()
      $installedFingerprint = "${bootstrapHash}:$runtimeHash"
      $candidateMarker = Join-Path $candidateDir $dependencyMarkerName
      Set-Content -LiteralPath $candidateMarker -Value $installedFingerprint -Encoding UTF8

      if (Test-Path -LiteralPath $previousDir) {
        Remove-Item -LiteralPath $previousDir -Recurse -Force
      }
      if (Test-Path -LiteralPath $runtimeDir) {
        Move-Item -LiteralPath $runtimeDir -Destination $previousDir
      }
      try {
        Move-Item -LiteralPath $candidateDir -Destination $runtimeDir
      }
      catch {
        if (!(Test-Path -LiteralPath $runtimeDir) -and
            (Test-Path -LiteralPath $previousDir -PathType Container)) {
          Move-Item -LiteralPath $previousDir -Destination $runtimeDir
        }
        throw
      }

      $script:venvPy = Join-Path $runtimeDir "Scripts\python.exe"
      $dependencyMarker = Join-Path $runtimeDir $dependencyMarkerName
      if (Test-Path -LiteralPath $previousDir) {
        Remove-Item -LiteralPath $previousDir -Recurse -Force
      }
      Write-Ok "Проверенные зависимости установлены"
    }
    catch {
      $script:venvPy = Join-Path $runtimeDir "Scripts\python.exe"
      $dependencyMarker = Join-Path $runtimeDir $dependencyMarkerName
      if (Test-Path -LiteralPath $candidateDir) {
        Remove-Item -LiteralPath $candidateDir -Recurse -Force
      }
      if (!(Test-Path -LiteralPath $runtimeDir) -and
          (Test-Path -LiteralPath $previousDir -PathType Container)) {
        Move-Item -LiteralPath $previousDir -Destination $runtimeDir
      }
      throw
    }
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
  if ($LASTEXITCODE -ne 0 -or $runtimeVersion.Trim() -ne "3.14") {
    throw "Окружение запуска повреждено или создано не для Python 3.14"
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
