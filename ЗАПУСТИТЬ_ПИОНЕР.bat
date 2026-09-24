@echo off
chcp 65001 > nul
title PIONEER Portal

if not exist ".venv\Scripts\python.exe" (
    echo First launch: creating the local Python environment...
    py -3 -m venv .venv
    if errorlevel 1 goto python_error
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto install_error

echo PIONEER is starting at http://127.0.0.1:5000
start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py
goto end

:python_error
echo Python 3 was not found. Install it from https://www.python.org/downloads/
pause
goto end

:install_error
echo Portal dependencies installation failed. Check your Internet connection and retry.
pause

:end
