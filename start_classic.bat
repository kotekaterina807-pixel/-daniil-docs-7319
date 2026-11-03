@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запасной (старый) интерфейс — если новое окно почему-то не открывается
start "" ".venv\Scripts\pythonw.exe" app_classic.py
