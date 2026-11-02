@echo off
title NMS DSV Management Utility
echo ====================================================
echo   Initializing Exosuit Vocal Diagnostic Interface...
echo ====================================================

set "ROOT=%~dp0"

REM Bootstrap: prefer the venv interpreter; create it (and install the UI
REM dependencies) on first run so the utility is always double-clickable.
REM Once the environment is healthy, the console dismisses itself and the
REM manager runs console-free via pythonw.
set "PYEXE=%ROOT%venv\Scripts\python.exe"
set "PYWEXE=%ROOT%venv\Scripts\pythonw.exe"
set "MGREXE=%ROOT%venv\Scripts\NMS_DSV_Manager.exe"
if not exist "%PYEXE%" goto bootstrap

REM The venv existing is not enough - verify the UI stack is actually present
REM (a partially cleaned venv must self-heal instead of crashing at boot).
REM Filesystem check only: spawning an interpreter here adds seconds of popup.
if not exist "%ROOT%venv\Lib\site-packages\PySide6\__init__.py" goto bootstrap

REM Healthy environment: hand off to the windowless interpreter and close.
goto handoff

:bootstrap
echo [First run] Creating the virtual environment and installing dependencies...
echo [First run] This can take a few minutes. Please wait.
if not exist "%PYEXE%" python -m venv "%ROOT%venv" || goto error
"%PYEXE%" -m pip install --upgrade pip --quiet
"%PYEXE%" -m pip install --prefer-binary -r "%ROOT%requirements.txt" || goto error

REM Bootstrap finished: verify, then hand off to the GUI without a console.
"%PYEXE%" -c "import PySide6" >nul 2>&1
if errorlevel 1 goto error
goto handoff

REM The named manager copy (a renamed interpreter for a clear Task Manager
REM entry) is preferred; the plain venv pythonw is the fallback.
:handoff
if exist "%MGREXE%" (
    start "" "%MGREXE%" "%ROOT%manager\main.py"
) else (
    start "" "%PYWEXE%" "%ROOT%manager\main.py"
)
exit /b 0

:error
echo.
echo [ERROR]: Bootstrap failed. Check your internet connection and Python install.
pause
exit /b 1

:error
echo.
echo [ERROR]: Bootstrap failed. Check your internet connection and Python install.
pause
exit /b 1
