@echo off
chcp 65001 >nul
setlocal
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 goto err
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto err
echo.
echo Готово. Запуск: .venv\Scripts\python.exe main.py
pause
exit /b 0
:err
echo Ошибка установки зависимостей.
pause
exit /b 1
