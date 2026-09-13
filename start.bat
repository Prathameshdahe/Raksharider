@echo off
setlocal EnableDelayedExpansion
title RoadWatch.AI — Launcher
cls

echo ============================================================
echo           ROADWATCH.AI — STAGED SYSTEM LAUNCHER
echo ============================================================
echo.

set ROOT=%~dp0

REM ── Resolve folder names (handles both old and new names) ──────────────────
if exist "%ROOT%backend\venv\Scripts\activate.bat" (
    set BACKEND=%ROOT%backend
) else if exist "%ROOT%DriveTrust-Backend\venv\Scripts\activate.bat" (
    set BACKEND=%ROOT%DriveTrust-Backend
) else (
    set BACKEND=
)

if exist "%ROOT%frontend\index.html" (
    set FRONTEND=%ROOT%frontend
) else if exist "%ROOT%roadwatch-pwa\index.html" (
    set FRONTEND=%ROOT%roadwatch-pwa
) else (
    set FRONTEND=
)

if exist "%ROOT%model-pipeline\worker.py" (
    set PIPELINE=%ROOT%model-pipeline
) else if exist "%ROOT%new ai pipeline\worker.py" (
    set PIPELINE=%ROOT%new ai pipeline
) else (
    set PIPELINE=
)

:: ════════════════════════════════════════════════════════════
:: STAGE 1 — PREFLIGHT CHECKS
:: ════════════════════════════════════════════════════════════
echo [Stage 1/5] Preflight checks...
echo.

:: Check backend venv
IF "%BACKEND%"=="" (
    echo   [FAIL] Backend venv not found.
    echo          Expected: backend\venv\ or DriveTrust-Backend\venv\
    pause
    exit /b 1
)
echo   [OK] Backend: %BACKEND%

:: Check frontend
IF "%FRONTEND%"=="" (
    echo   [FAIL] Frontend index.html not found.
    echo          Expected: frontend\ or roadwatch-pwa\
    pause
    exit /b 1
)
echo   [OK] Frontend: %FRONTEND%

:: Check pipeline
IF "%PIPELINE%"=="" (
    echo   [FAIL] Pipeline worker.py not found.
    echo          Expected: model-pipeline\worker.py or new ai pipeline\worker.py
    pause
    exit /b 1
)
echo   [OK] Pipeline: %PIPELINE%

:: Check backend .env
IF NOT EXIST "%BACKEND%\.env" (
    echo   [FAIL] Backend .env not found. Copy .env.example to .env and fill in values.
    pause
    exit /b 1
)
echo   [OK] Backend .env found

:: Check pipeline .env
IF NOT EXIST "%PIPELINE%\.env" (
    echo   [FAIL] Pipeline .env not found. Copy .env.example to .env and fill in values.
    pause
    exit /b 1
)
echo   [OK] Pipeline .env found

:: Check required .env keys
findstr /i "SUPABASE_URL" "%BACKEND%\.env" >nul 2>&1
IF ERRORLEVEL 1 (
    echo   [FAIL] SUPABASE_URL missing from backend .env
    pause
    exit /b 1
)
findstr /i "SUPABASE_SERVICE_ROLE_KEY" "%BACKEND%\.env" >nul 2>&1
IF ERRORLEVEL 1 (
    echo   [FAIL] SUPABASE_SERVICE_ROLE_KEY missing from backend .env
    pause
    exit /b 1
)
echo   [OK] Required .env keys present

:: Warn if ports already in use
netstat -an | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
IF NOT ERRORLEVEL 1 echo   [WARN] Port 8000 already in use
netstat -an | findstr ":5051 " | findstr "LISTENING" >nul 2>&1
IF NOT ERRORLEVEL 1 echo   [WARN] Port 5051 already in use

echo.
echo   All preflight checks passed. Starting services...
echo.

:: ════════════════════════════════════════════════════════════
:: STAGE 2 — START BACKEND
:: ════════════════════════════════════════════════════════════
echo [Stage 2/5] Starting Backend (FastAPI :8000)...

start "RoadWatch.AI ^ Backend (:8000)" cmd /k "title Backend (:8000) && cd /d \"%BACKEND%\" && call venv\Scripts\activate.bat && pip show fastapi >nul 2>&1 || pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

:: Poll /health until backend is up (max 30 seconds)
echo   Waiting for backend...
set BACKEND_UP=0
for /L %%i in (1,1,30) do (
    if !BACKEND_UP!==0 (
        timeout /t 1 /nobreak >nul
        powershell -Command "(Invoke-WebRequest -Uri 'http://localhost:8000/health' -UseBasicParsing -TimeoutSec 1 -ErrorAction SilentlyContinue).StatusCode" 2>nul | findstr "200" >nul 2>&1
        IF NOT ERRORLEVEL 1 set BACKEND_UP=1
    )
)
if !BACKEND_UP!==1 (
    echo   [OK] Backend is UP: http://localhost:8000
    echo        API docs:       http://localhost:8000/docs
) else (
    echo   [FAIL] Backend did not respond in 30s. Check the Backend window for errors.
    pause
    exit /b 1
)
echo.

:: ════════════════════════════════════════════════════════════
:: STAGE 3 — START FRONTEND
:: ════════════════════════════════════════════════════════════
echo [Stage 3/5] Starting Frontend PWA (:5051)...

start "RoadWatch.AI ^ Frontend (:5051)" cmd /k "title Frontend (:5051) && cd /d \"%FRONTEND%\" && python -m http.server 5051"

timeout /t 2 /nobreak >nul
netstat -an | findstr ":5051 " | findstr "LISTENING" >nul 2>&1
IF NOT ERRORLEVEL 1 (
    echo   [OK] Frontend is UP: http://localhost:5051
) ELSE (
    echo   [WARN] Port 5051 not yet listening (Python may still be starting)
)
echo.

:: ════════════════════════════════════════════════════════════
:: STAGE 4 — START AI WORKER
:: ════════════════════════════════════════════════════════════
echo [Stage 4/5] Starting AI Worker (queue polling)...

start "RoadWatch.AI ^ AI Worker" cmd /k "title AI Worker && cd /d \"%PIPELINE%\" && (if exist .venv\pyvenv.cfg call .venv\Scripts\activate.bat) && python worker.py --poll 10"

timeout /t 3 /nobreak >nul
echo   [OK] AI Worker window launched (logs show polling status)
echo.

:: ════════════════════════════════════════════════════════════
:: STAGE 5 — SUMMARY
:: ════════════════════════════════════════════════════════════
echo [Stage 5/5] All services started
echo.
echo   ┌─────────────────────────────────────────────────────┐
echo   │  Service          URL                               │
echo   ├─────────────────────────────────────────────────────┤
echo   │  Frontend PWA     http://localhost:5051             │
echo   │  Backend API      http://localhost:8000             │
echo   │  API Docs         http://localhost:8000/docs        │
echo   │  Admin Panel      http://localhost:5051/admin.html  │
echo   │  AI Worker        Polling Supabase (every 10s)      │
echo   └─────────────────────────────────────────────────────┘
echo.
echo   Close the service windows (or Ctrl+C in each) to stop.
echo.
start http://localhost:5051
pause
