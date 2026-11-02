@echo off
cd /d "%~dp0"
set PYQTGRAPH_QT_LIB=PySide2
set QT_OPENGL=software
"%~dp0runtime\python.exe" "%~dp0app.py"
if errorlevel 1 pause
