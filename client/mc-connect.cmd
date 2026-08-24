@echo off
REM Minecraft one-click connect launcher
REM Double-click this file. It runs mc-connect.ps1 with PowerShell.
title Minecraft Connect
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0mc-connect.ps1"
if errorlevel 1 pause
