@echo off
rem Starts the Proxy Chat bot for the Stellaris server (config.stellaris-server.toml).
rem start_bot.bat on its own uses config.toml, which is the test server.
cd /d "%~dp0"
.venv\Scripts\python.exe -m proxychat_bot --config config.stellaris-server.toml %*
pause
