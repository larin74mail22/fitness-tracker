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
echo Панель FITNESS Tracker: http://127.0.0.1:8765/  (остановить: Ctrl+C или закрыть окно)
start "" "http://127.0.0.1:8765/"
"%PY%" "%ROOT%dashboard.py" %*
echo.
echo Панель остановлена (код %errorlevel%). Если она не запустилась, причина написана выше.
pause
