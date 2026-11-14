@echo off
cd /d "%~dp0"
py monitor.py --test-notify
pause
