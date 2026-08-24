@echo off
setlocal
cd /d "%~dp0"

rem pythonw 로 실행하면 콘솔 창 없이 GUI 만 뜬다
where pythonw >nul 2>nul
if %errorlevel%==0 goto runw

where python >nul 2>nul
if %errorlevel%==0 goto run

where py >nul 2>nul
if %errorlevel%==0 goto runpy

goto nopython

:runw
start "" pythonw -m fastunnel gui
exit /b 0

:run
start "" python -m fastunnel gui
exit /b 0

:runpy
start "" pyw -m fastunnel gui
exit /b 0

:nopython
echo.
echo   Python 3.8 이상이 필요합니다. / Python 3.8+ is required.
echo   https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
echo   설치할 때 "Add python.exe to PATH" 를 반드시 체크하세요.
echo.
pause
exit /b 1
