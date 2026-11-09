@echo off
title TrueUp - PayPal sandbox test
cd /d "%~dp0"
echo.
echo  TrueUp - Day 1 PayPal sandbox test
echo  Paste your SANDBOX Client ID and Secret when asked (right-click pastes).
echo.
py -3 --version >nul 2>nul
if %errorlevel%==0 (
  py -3 day1_sandbox_test.py
  goto end
)
python -c "import sys" >nul 2>nul
if %errorlevel%==0 (
  python day1_sandbox_test.py
  goto end
)
echo  Could not find Python. Open day1_sandbox_test.py with Python instead.
:end
echo.
pause
