@echo off
REM ── RoadWatch.AI Backend Start Script (Windows) ─────────────────────────────
REM Run this from the DriveTrust-Backend directory

echo.
echo  ██████╗  ██████╗  █████╗ ██████╗ ██╗    ██╗ █████╗ ████████╗ ██████╗██╗  ██╗
echo  ██╔══██╗██╔═══██╗██╔══██╗██╔══██╗██║    ██║██╔══██╗╚══██╔══╝██╔════╝██║  ██║
echo  ██████╔╝██║   ██║███████║██║  ██║██║ █╗ ██║███████║   ██║   ██║     ███████║
echo  ██╔══██╗██║   ██║██╔══██║██║  ██║██║███╗██║██╔══██║   ██║   ██║     ██╔══██║
echo  ██║  ██║╚██████╔╝██║  ██║██████╔╝╚███╔███╔╝██║  ██║   ██║   ╚██████╗██║  ██║
echo  ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝
echo.
echo  DriveTrust Backend - Starting...
echo.

REM Check if venv exists
IF NOT EXIST "venv\Scripts\activate.bat" (
    echo [SETUP] Creating Python virtual environment...
    python -m venv venv
    IF ERRORLEVEL 1 (
        echo [ERROR] Failed to create venv. Make sure Python is installed.
        echo         Download from: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [OK] venv created.
)

REM Activate venv
call venv\Scripts\activate.bat
echo [OK] Virtual environment activated.

REM Install requirements if needed
pip show fastapi >nul 2>&1
IF ERRORLEVEL 1 (
    echo [SETUP] Installing dependencies first time...
    pip install -r requirements.txt
    IF ERRORLEVEL 1 (
        echo [ERROR] pip install failed. Check requirements.txt.
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed.
)

REM Check .env exists
IF NOT EXIST ".env" (
    echo [WARNING] No .env file found. Copying from .env.txt if available...
    IF EXIST ".env.txt" copy .env.txt .env
)

echo.
echo  Starting FastAPI server on http://localhost:8000
echo  Docs available at: http://localhost:8000/docs
echo  Health check:      http://localhost:8000/health
echo.

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
