@echo off
cd /d "%~dp0"
echo Starting JTQ on this PC for the same Wi-Fi only.
echo Public upload is the static-host folder — see static-host\README.md
echo Close this window or press Ctrl+C to stop the local server.
py -3 server.py
pause
