@echo off
rem Runs the HomeBank toolkit with the project's own Python, e.g.:  hb structure
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo The toolkit's Python environment was not found in this folder:
  echo   %~dp0
  echo Set it up once by running this in PowerShell in this folder:
  echo   python -m venv .venv
  echo or use the folder where you set it up before.
  exit /b 1
)
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv\Scripts\python.exe" -m hbkit %*
exit /b %ERRORLEVEL%
