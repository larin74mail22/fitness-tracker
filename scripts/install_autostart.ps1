# Автозапуск FITNESS Tracker при входе в Windows (Планировщик заданий, задача «FITNESS Tracker»).
#
# Задача запускает трекер БЕЗ окна консоли (pythonw tracker.pyw или FitnessTracker.exe) через 30 секунд после входа
# текущего пользователя. Второй экземпляр не создаётся (настройка задачи IgnoreNew и блокировка в самом трекере).
# Права администратора не нужны. Задача создаётся только этим скриптом и только по вашей команде.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1            # включить автозапуск
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Status    # посмотреть состояние
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove    # выключить автозапуск
#
# Параметры: -Mode Auto|Python|Exe (Auto — EXE, если он собран в dist\FitnessTracker, иначе pythonw),
#            -DryRun — только показать, что будет создано.
# Отключить можно и вручную: Планировщик заданий → Библиотека → «FITNESS Tracker» → Отключить или Удалить.

param(
    [switch]$Remove,
    [switch]$Status,
    [switch]$DryRun,
    [ValidateSet("Auto", "Python", "Exe")][string]$Mode = "Auto",
    [string]$TaskName = "FITNESS Tracker"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
# Прежний вариант автозапуска (окно наблюдения health_import_inbox.bat --watch) — ярлык в папке «Автозагрузка».
$oldLink = Join-Path ([Environment]::GetFolderPath("Startup")) "FITNESS Tracker - импорт Health Connect.lnk"

function Get-Task { Get-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction SilentlyContinue }

if ($Status) {
    $task = Get-Task
    if ($null -eq $task) { Write-Host "Автозапуск выключен: задачи «$TaskName» нет." }
    else {
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "Задача «$TaskName»: $($task.State)"
        Write-Host "Команда: $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
        Write-Host "Последний запуск: $($info.LastRunTime), результат: $($info.LastTaskResult)"
    }
    if (Test-Path -LiteralPath $oldLink) { Write-Host "Есть старый ярлык автозапуска наблюдения: $oldLink" }
    exit 0
}

if ($Remove) {
    if (Get-Task) {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath "\" -Confirm:$false
        Write-Host "Автозапуск выключен: задача «$TaskName» удалена. Работающий трекер не остановлен."
    } else { Write-Host "Задачи «$TaskName» нет, ничего не изменено." }
    exit 0
}

# Что запускать
$exe = Join-Path $root "dist\FitnessTracker\FitnessTracker.exe"
if ($Mode -eq "Exe" -or ($Mode -eq "Auto" -and (Test-Path -LiteralPath $exe))) {
    if (-not (Test-Path -LiteralPath $exe)) { throw "Не найден $exe. Соберите EXE (scripts\build_exe.ps1) или используйте -Mode Python." }
    $execute = $exe
    $arguments = ""
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } |
        Select-Object -First 1
    if ($null -eq $python) { throw "Не найден python в PATH (нужен тот же Python 3.11, которым tunnel-client запускает server.py)." }
    $execute = Join-Path (Split-Path -Parent $python.Source) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $execute)) { throw "Не найден $execute" }
    $script = Join-Path $root "tracker.pyw"
    if (-not (Test-Path -LiteralPath $script)) { throw "Не найден $script" }
    $arguments = "`"$script`""
}

Write-Host "Задача: «$TaskName» (пользователь $user, при входе в Windows, задержка 30 с)"
Write-Host "Команда: $execute $arguments"
Write-Host "Рабочая папка: $root"
if ($DryRun) { Write-Host "Пробный запуск: задача не создана."; exit 0 }

$action = if ($arguments) { New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $root }
          else { New-ScheduledTaskAction -Execute $execute -WorkingDirectory $root }
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -TaskPath "\" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "FITNESS Tracker: туннель и MCP-сервер, автоимпорт Health Connect, значок в трее. Создано scripts\install_autostart.ps1." |
    Out-Null
Write-Host "Готово: автозапуск включён. Трекер запустится при следующем входе в Windows."
Write-Host "Запустить сейчас, не перезагружаясь: Start-ScheduledTask -TaskName `"$TaskName`" (или двойной щелчок по tracker.pyw)."
Write-Host "Выключить: тот же скрипт с параметром -Remove."

if (Test-Path -LiteralPath $oldLink) {
    Remove-Item -LiteralPath $oldLink
    Write-Host "Удалён старый ярлык автозапуска наблюдения ($oldLink): наблюдение теперь входит в трекер."
}
