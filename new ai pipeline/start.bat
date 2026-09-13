@echo off
REM ── RoadWatch.AI Pipeline Worker Start Script ─────────────────────────────

echo.
echo  [RoadWatch.AI] AI Pipeline Worker (Queue Mode)
echo  ─────────────────────────────────────────────────
echo.

IF EXIST ".venv\pyvenv.cfg" (
    call .venv\Scripts\activate.bat
    echo [OK] Virtual environment activated.
) ELSE (
    echo [OK] Using Python environment.
)

pip show ultralytics >nul 2>&1
IF ERRORLEVEL 1 (
    echo [SETUP] Installing AI pipeline dependencies (first run - takes a few minutes)...
    pip install -r requirements.txt
)

pip show azure-storage-blob >nul 2>&1
IF ERRORLEVEL 1 (
    echo [SETUP] Installing Azure Storage SDK...
    pip install azure-storage-blob
)

echo.
echo  Starting AI Worker (queue polling mode)...
echo  Watching Supabase queue for new videos to process.
echo  Press Ctrl+C to stop.
echo.

python worker.py --poll 10
