@echo off
rem Portable launcher: no pip install required.
setlocal
pushd "%~dp0.." >nul
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python -m arb %*
set "RC=%ERRORLEVEL%"
popd >nul
exit /b %RC%
