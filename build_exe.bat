@echo off
setlocal
cd /d "%~dp0"

set "VPYTHON=venv\Scripts\python.exe"

if exist "%VPYTHON%" goto :venv_ok

echo == Creating venv ==
call :find_python
if errorlevel 1 exit /b 1
call %PYBASIS% -m venv venv
if errorlevel 1 (
    echo [ERROR] venv creation failed.
    exit /b 1
)
"%VPYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
echo == Installing requirements ==
"%VPYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] requirements install failed.
    exit /b 1
)

:venv_ok
if not exist "assets\nms_dsv.ico" (
    echo [ERROR] assets\nms_dsv.ico not found in the project root.
    exit /b 1
)

if not exist "state\bin\ffplay.exe" (
    echo [ERROR] state\bin\ffplay.exe missing - run the manager's Setup once first - this build bundles it.
    exit /b 1
)
if not exist "state\bin\wav2wem.exe" (
    echo [ERROR] state\bin\wav2wem.exe missing - run the manager's Setup once first - this build bundles it.
    exit /b 1
)

"%VPYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo == Installing PyInstaller - build only ==
    "%VPYTHON%" -m pip install pyinstaller==6.22.2
    if errorlevel 1 exit /b 1
)

echo == Building dist\NMS_DSV - onedir ==
"%VPYTHON%" -m PyInstaller NMS_DSV.spec --noconfirm
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

echo == Zipping dist\NMS_DSV -^> dist\NMS_DSV_win64.zip ==
powershell -NoProfile -Command "if (Test-Path dist\NMS_DSV_win64.zip) { Remove-Item dist\NMS_DSV_win64.zip -Force }; Compress-Archive -Path dist\NMS_DSV -DestinationPath dist\NMS_DSV_win64.zip"
if errorlevel 1 (
    echo [ERROR] zip step failed.
    exit /b 1
)

echo == DONE: dist\NMS_DSV_win64.zip ==
exit /b 0

:find_python
set "PYBASIS="
py -3.11 -c "print(1)">nul 2>nul && set "PYBASIS=py -3.11"
if defined PYBASIS exit /b 0
where python >nul 2>nul && set "PYBASIS=python"
if defined PYBASIS exit /b 0
echo [ERROR] No Python found.
exit /b 1
