@echo off
rem Kill any running ai-commit GUI before robocopy. Match by command line
rem (any python launcher: python.exe, pythonw.exe, pythonw3.12.exe, etc.)
rem and require Name to start with python so we never hit unrelated processes
rem (e.g. an editor that happens to have ai-commit-gui.py open).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'ai-commit-gui\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
robocopy "%~dp0." "%USERPROFILE%\OneDrive\Programs\ai-commit" /E /XD .git /PURGE /NFL /NDL /NJH /NJS
echo Deployed ai-commit to Programs\ai-commit
pause
