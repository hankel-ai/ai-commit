@echo off
setlocal
cd /d "%~dp0"

rem Isolated venv lives OUTSIDE OneDrive to avoid sync churn / "device busy".
set "VENV=%USERPROFILE%\.venvs\ai-commit"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PYW=%VENV%\Scripts\pythonw.exe"

if not exist "%VENV_PY%" (
    echo [setup] Creating virtual environment at %VENV% ...
    python -m venv "%VENV%" || goto :fail
    "%VENV_PY%" -m pip install --upgrade pip || goto :fail
    "%VENV_PY%" -m pip install -r requirements.txt || goto :fail
)

rem Self-heal: reinstall if any dependency is missing.
"%VENV_PY%" -c "import dearpygui, pystray, PIL" 2>nul || (
    echo [setup] Dependencies missing or changed, reinstalling...
    "%VENV_PY%" -m pip install -r requirements.txt || goto :fail
)

start "" "%VENV_PYW%" ai-commit-gui.py %*
goto :eof

:fail
echo [setup] Setup failed. See errors above.
pause
exit /b 1
