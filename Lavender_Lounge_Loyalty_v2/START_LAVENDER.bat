@echo off
title Lavender Lounge Loyalty
cd /d "%~dp0"

set "GMAIL_ADDRESS=lavenderlounge.leb@gmail.com"
set "ADMIN_EMAIL=lavenderlounge.leb@gmail.com"
set "ADMIN_PASSWORD=lavender-admin"

echo ==========================================
echo       LAVENDER LOUNGE LOYALTY
echo ==========================================
echo.
echo Admin:
echo   http://127.0.0.1:5000/admin/login
echo   Email: lavenderlounge.leb@gmail.com
echo   Password: lavender-admin
echo.
echo Staff:
echo   http://127.0.0.1:5000/staff/login
echo   Default email: staff@lavenderlounge.local
echo   Default password: lavender-staff
echo.
echo Customer:
echo   http://127.0.0.1:5000
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed.
    echo Install Python from https://www.python.org/downloads/
    echo Tick "Add Python to PATH".
    pause
    exit /b
)

if not exist ".venv" (
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -r requirements.txt >nul

echo.
echo Paste the GOOGLE APP PASSWORD for lavenderlounge.leb@gmail.com
echo Leave blank for test mode.
set /p "GMAIL_APP_PASSWORD=Google App Password: "

echo.
echo Starting...
start "" http://127.0.0.1:5000/admin/login
python app.py

pause
