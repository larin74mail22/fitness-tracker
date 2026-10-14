# Сборка FitnessTracker.exe (PyInstaller, папка dist\FitnessTracker). Необязательно: tracker.pyw работает и без сборки.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#
# Что делает:
# 1. создаёт отдельное окружение build\venv (глобальный Python и его пакеты не меняются) и ставит туда PyInstaller;
# 2. собирает dist\FitnessTracker\FitnessTracker.exe без окна консоли.
#
# В сборку НЕ попадают: база, data\, журналы, настройки, ключи и профиль tunnel-client. EXE ищет папку проекта
# (server.py и data\) выше себя, поэтому dist\ должна лежать внутри папки проекта. MCP-сервер по-прежнему
# запускается tunnel-client'ом командой «python ...\server.py» — сборка на MCP и туннель не влияет.
# Пересборка после обновления кода: запустить скрипт снова (старая папка dist\FitnessTracker заменяется).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root "build\venv"
$pyinstallerVersion = "6.11.1"

$python = Get-Command python -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } |
    Select-Object -First 1
if ($null -eq $python) { throw "Не найден python в PATH." }

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Создаю окружение сборки $venv"
    & $python.Source -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать $venv" }
}
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --disable-pip-version-check "pyinstaller==$pyinstallerVersion"
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyInstaller" }

Push-Location $root
try {
    & $vpy -m PyInstaller --noconfirm --clean --windowed --name FitnessTracker `
        --icon "fitness\launcher\icons\tracker_ok.ico" `
        --add-data "fitness\launcher\icons;fitness\launcher\icons" `
        --add-data "fitness\migrations;fitness\migrations" `
        --distpath "dist" --workpath "build\pyinstaller" --specpath "build" `
        "tracker.pyw"
    if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }
} finally { Pop-Location }

$exe = Join-Path $root "dist\FitnessTracker\FitnessTracker.exe"
Write-Host "Готово: $exe"
Write-Host "Проверка без запуска трекера: `"$exe`" --version не выводит текст (нет консоли); запустите EXE двойным щелчком."
