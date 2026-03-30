@echo off
robocopy "%~dp0." "%USERPROFILE%\OneDrive\Programs\ai-commit" /E /XD .git /PURGE /NFL /NDL /NJH /NJS
echo Deployed ai-commit to Programs\ai-commit
pause
