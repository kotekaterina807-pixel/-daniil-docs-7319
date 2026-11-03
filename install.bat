@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка генератора видео
echo ================================================
echo   Установка генератора документальных видео
echo ================================================
echo.

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo [1/3] Python не найден. Устанавливаю Python 3.12 через winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  ) else (
    echo.
    echo Не удалось установить Python автоматически.
    echo Скачайте его с https://www.python.org/downloads/ , при установке поставьте галочку
    echo "Add python.exe to PATH", затем снова запустите install.bat
    pause
    exit /b 1
  )
) else (
  echo [1/3] Python найден.
)

echo [2/3] Создаю окружение программы...
if not exist ".venv\Scripts\python.exe" (
  "%PY%" -m venv .venv 2>nul || %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Не удалось создать окружение. Сообщите текст ошибки выше.
  pause
  exit /b 1
)

echo [3/3] Устанавливаю компоненты (ffmpeg, обработка изображений, озвучка)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Ошибка установки компонентов. Проверьте интернет и запустите install.bat ещё раз.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe(); import tkinter, PIL, numpy, requests, edge_tts; print('Проверка компонентов: OK')"
if errorlevel 1 (
  echo Проверка не прошла. Сообщите текст ошибки выше.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import server; print('Ярлык DocuStudio на рабочем столе: ' + ('создан' if server.make_shortcut() else 'не создан'))"

echo.
echo Готово. Запускайте программу ярлыком DocuStudio на рабочем столе (или start.bat)
pause
