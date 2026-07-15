@echo off
chcp 65001 >nul
cd /d "%~dp0"
title weather newShun - AMeDAS mini dashboard

echo ==================================================
echo    weather newShun  ( AMeDAS mini dashboard )
echo ==================================================
echo.

rem --- first run: install required packages if missing ---
py -c "import streamlit, requests, pandas" 1>nul 2>nul
if errorlevel 1 (
  echo  [first run] installing required packages ...
  py -m pip install -r requirements.txt
  echo.
)

echo  Starting the app... your browser will open shortly.
echo.
echo  [ To STOP the app, just close this window. ]
echo.

py -m streamlit run "minadas.py"

echo.
echo  The app has stopped. Press any key to close.
pause >nul
