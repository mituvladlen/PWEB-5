@echo off
python "%~dp0go2web.py" %* 2>nul
if %errorlevel% neq 0 (
    python3 "%~dp0go2web.py" %* 2>nul
    if %errorlevel% neq 0 (
        py "%~dp0go2web.py" %*
    )
)
