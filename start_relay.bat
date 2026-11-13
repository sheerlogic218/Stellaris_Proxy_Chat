@echo off
rem Starts the Proxy Chat relay (pair once first: see docs). Leave it running while you host.
cd /d "%~dp0"
.venv\Scripts\python.exe -m proxychat_relay run %*
pause
