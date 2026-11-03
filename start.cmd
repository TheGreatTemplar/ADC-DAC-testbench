@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" -c "import numpy, pyqtgraph, serial; from qt_compat import QtCore, QtGui, QtWidgets" >nul 2>&1
if errorlevel 1 goto setup
goto run

:setup
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
    if errorlevel 1 (
        pause
        exit /b 1
    )

:run
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
