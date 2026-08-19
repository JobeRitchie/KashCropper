@echo off
title Kash Cropper
echo ================================================
echo          Kash Cropper - Video Cropper
echo ================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python was not found.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Installing / verifying dependencies...
echo.
pip install -r "%~dp0requirements.txt" --quiet
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Dependency installation failed.
    echo Try running:  pip install opencv-python Pillow
    echo.
    pause
    exit /b 1
)

echo Starting Kash Cropper...
echo.
python "%~dp0cropper.py"

if %errorlevel% neq 0 (
    echo.
    echo The application exited with an error.
    pause
)
