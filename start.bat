@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала запустите install.bat
  pause
  exit /b 1
)
if exist ".venv\Scripts\DocuStudio.exe" if exist ".venv\Scripts\DocuStudio.ok" (
  start "" ".venv\Scripts\DocuStudio.exe" app.py
  exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" app.py
