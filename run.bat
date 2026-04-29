@echo off
REM Launches Zotero PDF Export. Tries the Python launcher first (recommended on
REM Windows), then falls back to plain `python` on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0zotero_export.py" %*
) else (
    python "%~dp0zotero_export.py" %*
)
if errorlevel 1 pause
