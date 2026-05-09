@echo off
REM Запуск TManager одним кликом.
REM Делает три вещи: переходит в свою папку, активирует venv, запускает bot.py.
REM В конце pause — окно не закроется, если бот упадёт, и ты увидишь traceback.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [start.bat] venv не найден. Создаю и ставлю зависимости...
    py -m venv venv || goto :error
    call venv\Scripts\activate.bat
    pip install -r requirements.txt || goto :error
) else (
    call venv\Scripts\activate.bat
)

echo [start.bat] Запускаю бота. Останов — Ctrl+C.
python bot.py

:error
echo.
echo [start.bat] Бот завершён. Нажми любую клавишу, чтобы закрыть окно.
pause >nul
