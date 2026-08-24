@echo off
REM Tunnel connect launcher
REM Double-click this file.
title Tunnel Connect
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0connect.ps1"
if errorlevel 1 pause
