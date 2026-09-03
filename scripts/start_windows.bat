@echo off
REM ============================================================
REM  Ahmed Reaction Studio - Windows startup (zero npm / zero Node)
REM  Creates a venv, installs Python deps, checks FFmpeg,
REM  launches the local server and opens your browser.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo === Ahmed Reaction Studio (Windows) ===

REM ---- Python check -------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found. Install Python 3.11+ from https://python.org
    echo         and make sure "Add python.exe to PATH" is checked during setup.
    pause & exit /b 1
)

REM ---- venv ----------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] venv creation failed. & pause & exit /b 1 )
)
call ".venv\Scripts\activate.bat"

REM ---- dependencies ---------------------------------------------------
echo Installing Python dependencies (first run only)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

REM ---- FFmpeg check (self-healing message) ---------------------------
set "FFCMD=ffmpeg"
where ffmpeg >nul 2>nul
if errorlevel 1 (
    if exist "C:\ffmpeg\bin\ffmpeg.exe" ( set "FFCMD=C:\ffmpeg\bin\ffmpeg.exe" )
)
echo FFmpeg: !FFCMD!
!FFCMD! -version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [WARN] FFmpeg was not found. The studio still runs, but proxies and
    echo        exports need FFmpeg:
    echo            winget install Gyan.FFmpeg
    echo        (or download from https://ffmpeg.org/download.html)
    echo.
)

REM ---- run diagnostics -------------------------------------------------
python scripts\diagnostics.py

REM ---- config ----------------------------------------------------------
if not exist "config.json" (
    copy "config.example.json" "config.json" >nul
    echo Created config.json from config.example.json
)

REM ---- launch -----------------------------------------------------------
echo.
echo Starting local server on http://127.0.0.1:8642  (LAN reachable on 0.0.0.0)
echo Press Ctrl+C in this window to stop the studio.
start "" http://127.0.0.1:8642
python -m uvicorn app.server:build_app --host 0.0.0.0 --port 8642

endlocal
