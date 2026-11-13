@echo off
rem Starts the Proxy Chat bot from this folder, wherever it's launched from.
cd /d "%~dp0"
.venv\Scripts\python.exe -m proxychat_bot %*
pause
