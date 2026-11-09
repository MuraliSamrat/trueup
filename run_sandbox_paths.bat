@echo off
title TrueUp - live sandbox: 3 settlement paths
cd /d "%~dp0"
echo.
echo  TrueUp - live PayPal sandbox run (small change / big change / cancel)
echo  Installing requirements (first run only takes a moment)...
py -3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo  Could not install requirements. Send a screenshot to Claude.
  goto end
)
py -3 scripts\sandbox_paths.py
:end
echo.
pause
