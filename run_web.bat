@echo off
title TrueUp web app (http://127.0.0.1:8000)
cd /d "%~dp0"
echo.
echo  TrueUp web app: http://127.0.0.1:8000
echo  Technician: http://127.0.0.1:8000/tech    Owner: http://127.0.0.1:8000/owner
echo  Staff login: user "owner" (or "tech"), password from STAFF_PASSWORD in .env (default trueup-demo)
echo.
"C:\Users\MuraliNew\AppData\Local\Programs\Python\Python314\python.exe" -m uvicorn app.main:build_web --factory --port 8000
pause
