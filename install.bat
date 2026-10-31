@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Убедитесь, что установлен Python 3.10+.
  pause
  exit /b 1
)
echo Зависимости установлены. Для запуска выполните: py port_bot.py
pause
