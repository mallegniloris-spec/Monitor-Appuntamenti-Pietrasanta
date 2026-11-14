@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
py -m playwright install chromium
if not exist .env copy .env.example .env
pause
