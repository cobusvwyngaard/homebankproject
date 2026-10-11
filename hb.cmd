@echo off
rem Runs the HomeBank toolkit with the project's own Python, e.g.:  hb structure
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv\Scripts\python.exe" -m hbkit %*
exit /b %ERRORLEVEL%
