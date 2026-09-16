@echo off
cd /d "%~dp0"
pythonw completion_monitor.py
if errorlevel 1 python completion_monitor.py
