@echo off
REM Double-click this to install (or repair) the tracker. Nothing to type.
REM
REM A Pokemon TCG Live update removes the tracker, and the tracker puts itself back. Run
REM this again only if it ever tells you it could not.
REM
REM -ExecutionPolicy Bypass applies to this one process only; it does not change any
REM machine setting. It is needed because the default policy blocks local .ps1 files.

title cbrew Tracker - Install

REM install.ps1 prints the whole story, success or failure, in plain words. This file
REM adds nothing on top of it - it only keeps the window open until a key is pressed.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set RC=%ERRORLEVEL%

echo.
pause
exit /b %RC%
