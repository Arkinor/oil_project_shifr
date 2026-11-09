@echo off
setlocal EnableExtensions EnableDelayedExpansion
set APP=PIIMasker

if not exist ".venv\Scripts\python.exe" (
    echo Creating venv...
    python -m venv .venv
    if errorlevel 1 goto err
)
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto err

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto err

set "TRAY_ARGS="
set "TRAY_HIDDEN="
python -c "import pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo [WARN] pystray/Pillow not found - building without tray.
) else (
    set "TRAY_ARGS=--hidden-import=pystray --hidden-import=PIL --hidden-import=PIL.Image --hidden-import=PIL.ImageDraw"
    set "TRAY_HIDDEN=--collect-submodules pystray --collect-data pystray"
)

set "ICON_ARG="
set "ICON_DATA="
if exist "icon.ico" (
    set "ICON_ARG=--icon icon.ico"
    set "ICON_DATA=--add-data icon.ico;."
) else (
    echo [WARN] icon.ico not found - building without icon.
)

echo Building exe...
pyinstaller --noconfirm --clean --onefile --noconsole --name %APP% %ICON_ARG% --add-data "index.html;." %ICON_DATA% --collect-submodules uvicorn --collect-submodules langchain_core --collect-submodules langchain_openai --collect-submodules openai --collect-data faker --collect-submodules pymupdf --collect-submodules openpyxl %TRAY_HIDDEN% %TRAY_ARGS% --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.loops.asyncio --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.http.h11_impl --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --hidden-import uvicorn.lifespan.off main.py
if errorlevel 1 goto err

if exist "settings.json" (
    copy /y "settings.json" "dist\settings.json" >nul
    echo settings.json copied to dist\
) else (
    echo settings.json will be created on first run.
)

echo.
echo Done: dist\%APP%.exe
echo Site: http://127.0.0.1:9321
pause
exit /b 0

:err
echo.
echo [ERROR] Build failed.
pause
exit /b 1