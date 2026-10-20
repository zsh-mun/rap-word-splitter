@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Word Cutter - RAP Sample Machine
set "PYTHONUTF8=1"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Preparing the local runtime. This only happens once...
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ and run this file again.
    pause
    exit /b 1
  )
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create the local runtime.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install dependencies. Check the network and try again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 (
  echo.
  echo The program stopped with an error. See the message above.
  pause
)
