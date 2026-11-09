@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set APP=PIIMasker

rem ---------------------------------------------------------------- venv
if not exist .venv (
    echo Создание venv...
    python -m venv .venv
    if errorlevel 1 goto err
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto err

rem ---------------------------------------------------------------- зависимости
echo Установка зависимостей...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto err

rem Трей работает только если pystray и Pillow реально установлены
python -c "import pystray, PIL" 2>nul
if errorlevel 1 (
    echo [!] pystray/Pillow не найдены — собираю без трея.
    echo     Чтобы включить трей, добавьте в requirements.txt:
    echo     pystray ^>=0.19
    echo     pillow ^>=10.0
    set TRAY_ARGS=
    set TRAY_HIDDEN=
) else (
    set TRAY_ARGS=--hidden-import=pystray --hidden-import=PIL --hidden-import=PIL.Image --hidden-import=PIL.ImageDraw
    set TRAY_HIDDEN=--collect-submodules pystray --collect-data pystray
)

rem ---------------------------------------------------------------- иконка
if exist icon.ico (
    set ICON_ARG=--icon icon.ico
    set ICON_DATA=--add-data "icon.ico;."
) else (
    echo [!] icon.ico не найден — собираю со стандартной иконкой и без встроенной картинки для трея.
    set ICON_ARG=
    set ICON_DATA=
)

rem ---------------------------------------------------------------- сборка
echo Сборка exe...
pyinstaller --noconfirm --clean --onefile --noconsole ^
    --name %APP% %ICON_ARG% ^
    --add-data "index.html;." %ICON_DATA% ^
    --collect-submodules uvicorn ^
    --collect-submodules langchain_core ^
    --collect-submodules langchain_openai ^
    --collect-submodules openai ^
    --collect-data faker ^
    --collect-submodules pymupdf ^
    --collect-submodules openpyxl ^
    %TRAY_HIDDEN% %TRAY_ARGS% ^
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

rem ---------------------------------------------------------------- settings.json
rem Файл может не существовать до первого запуска main.py — не считаем это ошибкой
if exist settings.json (
    copy /y settings.json dist\settings.json >nul
    echo settings.json скопирован в dist\
) else (
    echo settings.json пока нет — он будет создан автоматически при первом запуске exe.
)

echo.
echo Готово: dist\%APP%.exe
echo Рядом с exe создаются settings.json и папка data.
echo Сайт откроется автоматически: http://127.0.0.1:9321
echo Иконка в трее: меню "Открыть сайт" / "Папка результатов" / "Выход".
echo Для доступа с других компьютеров используйте IP этого компьютера.
pause
exit /b 0

:err
echo.
echo Ошибка сборки. Смотрите сообщения выше.
pause
exit /b 1