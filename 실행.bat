@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Public Data Catalog

REM ---------------------------------------------------------------
REM  This launcher stays ASCII on purpose.
REM  cmd.exe misreads UTF-8 batch files that contain Korean text and
REM  can stop halfway, so every Korean message is printed by run.py.
REM ---------------------------------------------------------------

set "PY="
where python >nul 2>&1 && set "PY=python"
if defined PY goto haspy
where py >nul 2>&1 && set "PY=py -3"
if defined PY goto haspy
goto nopython

:haspy
%PY% "%~dp0run.py" %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [!] Finished with errors. Exit code %RC%
  echo.
  pause
)
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found on this PC.
echo.
echo Install Python 3 from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
