@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0"
set "PY=python"
where python >nul 2>nul || (if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe") else (
  echo ОШИБКА: не найден Python. Установите Python 3.11 или добавьте его в PATH.
  pause
  exit /b 1
))
rem Импорт выгрузок Health Connect: новые файлы из data\health_syncthing (папка Syncthing, только чтение)
rem копируются в data\health_inbox и импортируются оттуда.
rem   health_import_inbox.bat                 обработать новые файлы один раз
rem   health_import_inbox.bat --watch         наблюдать за папкой, пока окно открыто
rem   health_import_inbox.bat --retry-failed  вернуть файлы из health_failed и обработать снова
rem   health_import_inbox.bat --dry-run       проверить без записи
"%PY%" "%ROOT%health_inbox.py" %*
set "CODE=%errorlevel%"
echo.
if "%CODE%"=="0" echo Готово.
if "%CODE%"=="1" echo Есть ошибки: подробности выше и в data\health_failed\*.error.txt
if "%CODE%"=="2" echo Есть файлы, которые ждут (пропущены номера): подробности выше.
if "%CODE%"=="3" echo Папку уже обрабатывает другое окно.
pause
exit /b %CODE%
