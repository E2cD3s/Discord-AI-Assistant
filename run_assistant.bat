@echo off
setlocal enabledelayedexpansion

REM Determine project root based on script location
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR%"=="" set "SCRIPT_DIR=.\"

REM Default configuration file path can be overridden by passing it as first argument
set "CONFIG_FILE=%SCRIPT_DIR%config.yaml"
if not "%~1"=="" (
    set "CONFIG_FILE=%~1"
) else (
    if not exist "%CONFIG_FILE%" (
        set "CONFIG_FILE=%SCRIPT_DIR%config.example.yaml"
    )
)

if not exist "%CONFIG_FILE%" (
    echo [ERROR] Could not find configuration file.
    echo         Create config.yaml or pass a path as the first argument.
    exit /b 1
)

REM Activate the local virtual environment if it exists
set "VENV_ACTIVATE=%SCRIPT_DIR%.venv\Scripts\activate.bat"
if exist "%VENV_ACTIVATE%" (
    call "%VENV_ACTIVATE%"
) else (
    echo [INFO] No virtual environment found at .venv\Scripts\activate.bat. Using system Python.
)

python -m src.main --config "%CONFIG_FILE%"

endlocal
