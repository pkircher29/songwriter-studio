@echo off
rem One click: starts Houseband in the background and opens it in your browser.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app\server.py"
