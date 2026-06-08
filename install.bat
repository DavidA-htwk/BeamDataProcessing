@echo off
REM Creates a virtual environment and installs all dependencies.
REM Run once after cloning: double-click install.bat or run from cmd.

echo === BeamDataProcessing Setup ===

python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: Python not found. Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

IF NOT EXIST ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
) ELSE (
    echo Virtual environment already exists, skipping creation.
)

echo Activating and installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo === Setup complete ===
echo To run the application:
echo   .venv\Scripts\activate
echo   python Data_handling.py
pause
