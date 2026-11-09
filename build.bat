@echo off
chcp 65001 >nul
setlocal
set APP=PIIMasker

if not exist .venv (
    echo Создание venv...
    python -m venv .venv
    if errorlevel 1 goto err
)
call .venv\Scripts\activate.bat

echo Установка зависимостей...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto err

echo Сборка exe...
pyinstaller --noconfirm --clean --onefile --noconsole ^
    --name %APP% --icon icon.ico ^
    --add-data "index.html;." ^
    --collect-submodules uvicorn ^
    --collect-submodules langchain_core ^
    --collect-submodules langchain_openai ^
    --collect-submodules openai ^
    --collect-data faker ^
    --collect-submodules pymupdf ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.loops.asyncio ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import uvicorn.lifespan.off ^
    main.py
if errorlevel 1 goto err

copy /y settings.json dist\settings.json >nul
echo.
echo Готово: dist\%APP%.exe
echo Рядом с exe создаются settings.json и папка data.
echo Сайт откроется автоматически: http://127.0.0.1:9321
echo Для доступа с других компьютеров используйте IP этого компьютера.
pause
exit /b 0

:err
echo.
echo Ошибка сборки. Смотрите сообщения выше.
pause
exit /b 1
