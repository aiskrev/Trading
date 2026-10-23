@echo off
rem Starts the control room on http://127.0.0.1:8765/ (this computer only) and opens the browser.
rem Close this window or press Ctrl+C to stop it.
cd /d "%~dp0\.."
set PYTHONPATH=src;.deps
where py >nul 2>nul && (py -3.12 -c "" >nul 2>nul && (py -3.12 -m ui %* & goto :eof))
where py >nul 2>nul && (py -m ui %* & goto :eof)
python -m ui %*
