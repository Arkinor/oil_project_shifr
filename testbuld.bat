@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
set APP=PIIMaskerTest

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

echo Building test exe...
pyinstaller --noconfirm --clean --onefile --console --name %APP% --collect-submodules langchain_core --collect-submodules langchain_openai --collect-submodules openai --collect-submodules pymupdf --collect-submodules openpyxl test.py
if errorlevel 1 goto err

if exist "settings.json" (
    copy /y "settings.json" "dist\settings.json" >nul
    echo settings.json copied to dist\
) else (
    echo settings.json will be created on first run.
)

echo.
echo Done: dist\%APP%.exe
echo Results will be saved to dist\test.json
pause
exit /b 0

:err
echo.
echo [ERROR] Build failed.
pause
exit /b 1
