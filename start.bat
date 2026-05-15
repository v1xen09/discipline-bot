@echo off

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [start.bat] venv not found, installing dependencies...
    py -m venv venv || goto :error
    call venv\Scripts\activate.bat
    pip install -r requirements.txt || goto :error
) else (
    call venv\Scripts\activate.bat
)

echo [start.bat] starting (Ctrl+C to stop)
python bot.py

:error
echo.
echo [start.bat] stopped, press any key...
pause >nul
