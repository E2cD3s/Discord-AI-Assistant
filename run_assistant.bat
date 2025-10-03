@echo off
setlocal enabledelayedexpansion

REM Determine project root based on script location
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR%"=="" set "SCRIPT_DIR=.\"

set "DO_SETUP=0"
set "CONFIG_OVERRIDE="

:parse_args
if "%~1"=="" goto after_args
if /I "%~1"=="--setup" (
    set "DO_SETUP=1"
    shift
    goto parse_args
)
if /I "%~1"=="--config" (
    shift
    if "%~1"=="" (
        echo [ERROR] Missing value for --config.
        exit /b 1
    )
    set "CONFIG_OVERRIDE=%~1"
    shift
    goto parse_args
)
if "%CONFIG_OVERRIDE%"=="" (
    set "CONFIG_OVERRIDE=%~1"
    shift
    goto parse_args
)
echo [ERROR] Unknown argument: %~1
exit /b 1

:after_args

REM Default configuration file path can be overridden by --config or the first positional argument
if not "%CONFIG_OVERRIDE%"=="" (
    set "CONFIG_FILE=%CONFIG_OVERRIDE%"
) else (
    set "CONFIG_FILE=%SCRIPT_DIR%config.yaml"
    if not exist "%CONFIG_FILE%" (
        set "CONFIG_FILE=%SCRIPT_DIR%config.example.yaml"
    )
)

if not exist "%CONFIG_FILE%" (
    echo [ERROR] Could not find configuration file.
    echo         Create config.yaml or pass a path as the first argument.
    exit /b 1
)

set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "REQUIREMENTS_FILE=%SCRIPT_DIR%requirements.txt"

if "%DO_SETUP%"=="1" (
    if not exist "%VENV_ACTIVATE%" (
        echo [INFO] Creating virtual environment at %VENV_DIR%
        set "PYTHON_CMD=python"
        where python >nul 2>nul
        if errorlevel 1 (
            where py >nul 2>nul
            if errorlevel 1 (
                echo [ERROR] Could not find Python or the py launcher on PATH.
                exit /b 1
            )
            set "PYTHON_CMD=py -3"
        )
        %PYTHON_CMD% -m venv "%VENV_DIR%"
    )
)

if exist "%VENV_ACTIVATE%" (
    call "%VENV_ACTIVATE%"
) else (
    echo [INFO] No virtual environment found at .venv\Scripts\activate.bat. Using system Python.
)

if "%DO_SETUP%"=="1" (
    if not exist "%REQUIREMENTS_FILE%" (
        echo [ERROR] Requirements file not found at %REQUIREMENTS_FILE%.
        exit /b 1
    )
    echo [INFO] Upgrading pip
    python -m pip install --upgrade pip
    echo [INFO] Installing dependencies from %REQUIREMENTS_FILE%
    python -m pip install -r "%REQUIREMENTS_FILE%"
)

python -m src.main --config "%CONFIG_FILE%"

endlocal
