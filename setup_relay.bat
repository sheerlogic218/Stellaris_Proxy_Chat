@echo off
rem One-off setup of the Proxy Chat relay on a game host's PC: a Python environment with
rem only the relay's hash-checked packages. Needs Python 3.11+ from python.org.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements\relay.txt
echo.
echo Done. Ask an admin for a code with /proxychat host, then run what it shows, e.g.:
echo   .venv\Scripts\python.exe -m proxychat_relay pair https://server.example CODE --label "Your name"
echo After that, start_relay.bat whenever you host.
pause
