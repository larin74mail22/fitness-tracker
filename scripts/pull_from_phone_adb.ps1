# Резервный способ передачи: забрать файлы выгрузки с телефона по USB (adb) во входящую папку FITNESS Tracker.
#
# Нужно: на телефоне включена «Отладка по USB», телефон подключён кабелем и разрешил отладку этому компьютеру,
# установлено приложение FITNESS Health Export отладочной сборки (app-debug.apk: run-as работает только с ней).
# Скрипт только читает файлы приложения на телефоне (adb exec-out run-as ... cat) и ничего на телефоне не меняет.
# Уже забранные файлы (они есть во входящей папке, в health_processed или в health_failed) повторно не копируются.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File scripts\pull_from_phone_adb.ps1 [-Import]
#   -Import  после копирования сразу запустить импорт (health_inbox.py).

param(
    [switch]$Import,
    [string]$Adb = "adb.exe",
    [string]$Package = "local.fitness.healthexport"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $root "data"
$inbox = Join-Path $data "health_inbox"
$known = @("health_inbox", "health_processed", "health_failed") | ForEach-Object { Join-Path $data $_ }

if (-not (Test-Path $Adb)) {
    $found = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($found) { $Adb = $found.Source } else { throw "Не найден adb.exe ($Adb). Укажите путь параметром -Adb." }
}

$devices = & $Adb devices | Select-Object -Skip 1 | Where-Object { $_ -match "\S" }
$ready = @($devices | Where-Object { $_ -match "\tdevice$" })
if ($ready.Count -eq 0) {
    Write-Host "Телефон не найден или не разрешил отладку. Вывод adb devices:" -ForegroundColor Yellow
    $devices | ForEach-Object { Write-Host "  $_" }
    Write-Host "Подключите телефон кабелем, включите «Отладку по USB» и подтвердите запрос на телефоне."
    exit 1
}
if ($ready.Count -gt 1) { throw "Подключено несколько устройств: оставьте одно." }

# Windows PowerShell 5.1 при $ErrorActionPreference = "Stop" превращает любой вывод adb в stderr (2>&1) в
# исключение NativeCommandError, и понятное сообщение ниже не показывалось бы: на время вызова — "Continue".
$ErrorActionPreference = "Continue"
$names = & $Adb exec-out run-as $Package ls files/exports 2>&1 | ForEach-Object { "$_" }
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0 -or ($names -join "`n") -match "not debuggable|unknown package|No such file") {
    Write-Host "Не удалось прочитать файлы приложения: $($names -join ' ')" -ForegroundColor Yellow
    Write-Host "Нужна отладочная сборка FITNESS Health Export и хотя бы один экспорт на телефоне."
    exit 1
}
$names = @($names | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^fitness-health-[A-Za-z0-9-]+\.json$' })
New-Item -ItemType Directory -Force -Path $inbox | Out-Null

$copied = 0
foreach ($name in $names) {
    $already = $known | Where-Object { Test-Path (Join-Path $_ $name) }
    if ($already) { continue }
    $part = Join-Path $inbox ".$name.tmp"
    # cmd /c: перенаправление без перекодировки (PowerShell 5 переписал бы байты в UTF-16).
    cmd /c "`"$Adb`" exec-out run-as $Package cat files/exports/$name > `"$part`""
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $part) -or (Get-Item $part).Length -eq 0) {
        Remove-Item -Force -ErrorAction SilentlyContinue $part
        Write-Host "Не удалось скопировать $name" -ForegroundColor Yellow
        continue
    }
    $remote = (& $Adb exec-out run-as $Package sha256sum files/exports/$name) -split "\s+" | Select-Object -First 1
    $local = (Get-FileHash -Algorithm SHA256 $part).Hash.ToLower()
    if ($remote -and $remote -match '^[0-9a-f]{64}$' -and $remote -ne $local) {
        Remove-Item -Force $part
        Write-Host "Контрольная сумма $name не совпала, файл не принят: повторите." -ForegroundColor Yellow
        continue
    }
    Move-Item -LiteralPath $part -Destination (Join-Path $inbox $name)
    Write-Host "Скопирован: $name"
    $copied++
}
Write-Host "Новых файлов скопировано: $copied (всего на телефоне: $($names.Count))."

if ($Import) {
    $env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
    & python (Join-Path $root "health_inbox.py")
    exit $LASTEXITCODE
}
