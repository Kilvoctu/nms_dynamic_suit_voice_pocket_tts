@echo off
setlocal enabledelayedexpansion

echo.
echo === HF Model Download ===

echo --- Entering download_model --- https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/params?download=true
set "MODELURL=https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf"
set "PARAMSURL=https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/params"
set "MODELDIR=assets\qwen3_17b_q8"

if not exist "%MODELDIR%" mkdir "%MODELDIR%"

set "TARGETFILE=%MODELDIR%\Qwen3-1.7B-Q8_0.gguf"
set "TARGETPARAMS=%MODELDIR%\params"

REM Only download if the file doesn't already exist
if exist "%TARGETFILE%" (
    echo Model already exists at %TARGETFILE%, skipping download.
) else (
    call powershell -File download_model.ps1 -url "%MODELURL%" -outfile "%TARGETFILE%"
    call powershell -File download_model.ps1 -url "%PARAMSURL%" -outfile "%TARGETPARAMS%"
)

echo === Download Complete ===
pause
exit /b
