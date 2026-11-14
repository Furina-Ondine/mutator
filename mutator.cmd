@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if exist "%PY%" goto install
where py >nul 2>nul
if errorlevel 1 (
  python -m venv "%ROOT%.venv"
) else (
  py -3 -m venv "%ROOT%.venv"
)
if errorlevel 1 exit /b %errorlevel%
:install
if exist "%ROOT%.venv\Scripts\mutator.exe" goto run
"%PY%" -m pip install -e "%ROOT%." 1>&2
if errorlevel 1 exit /b %errorlevel%
:run
"%PY%" -m mutator %*
exit /b %errorlevel%
