@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PYTHON_CMD=python
set LOCAL_PYTHON=python_embed\python.exe
set VENV_DIR=venv
set VENV_PYTHON=%VENV_DIR%\Scripts\python.exe

echo ===============================================
echo          NextStar - College Planner
echo ===============================================
echo.

if exist "%LOCAL_PYTHON%" (
    set PYTHON_EXE=%LOCAL_PYTHON%
    goto install_deps
)

if exist "%VENV_PYTHON%" (
    set PYTHON_EXE=%VENV_PYTHON%
    goto install_deps
)

if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment...
    %PYTHON_CMD% -m venv %VENV_DIR%
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
)

if exist "%VENV_PYTHON%" (
    set PYTHON_EXE=%VENV_PYTHON%
) else (
    echo [ERROR] Python not found. Please install Python or run setup first.
    pause
    exit /b 1
)

:install_deps
echo [INFO] Installing dependencies...
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo [INFO] Python Path: %PYTHON_EXE%
echo [INFO] Starting service...
echo [INFO] Visit: http://localhost:5000 after startup
echo [INFO] Press Ctrl+C to stop the service
echo.

"%PYTHON_EXE%" app.py

pause
