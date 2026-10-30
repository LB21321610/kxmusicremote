@echo off
setlocal
title Apple Music Remote - Stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Remote-With-Translation.ps1" -Action Stop
set "remoteResult=%ERRORLEVEL%"
if not "%remoteResult%"=="0" (
    echo.
    pause
    exit /b %remoteResult%
)
rem Success: keep the short summary on screen for a moment, then close.
timeout /t 3 /nobreak >nul 2>&1
exit /b 0
