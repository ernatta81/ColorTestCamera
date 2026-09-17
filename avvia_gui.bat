@echo off
rem Avvia l interfaccia grafica del test LED usando il Python del venv
cd /d "%~dp0"
".venv\Scripts\python.exe" led_gui.py
if errorlevel 1 pause
