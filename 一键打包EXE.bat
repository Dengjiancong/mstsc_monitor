@echo off
setlocal
cd /d "%~dp0"
set "PAUSE_AT_END=1"
if /i "%~1"=="--no-pause" set "PAUSE_AT_END=0"

if not exist "sikadi.ico" (
    echo Missing sikadi.ico icon.
    if "%PAUSE_AT_END%"=="1" pause
    exit /b 1
)

python -c "import PyInstaller, lark_channel" >nul 2>&1
if errorlevel 1 (
    echo Installing build dependencies...
    python -m pip install -r "build-requirements.txt"
    if errorlevel 1 (
        echo Dependency installation failed. Check Python and network access.
        if "%PAUSE_AT_END%"=="1" pause
        exit /b 1
    )
)

echo Building MSTSC_Monitor.exe...
python -m PyInstaller --noconfirm --onefile --windowed --icon "sikadi.ico" --add-data "sikadi.ico:." --collect-all lark_channel --name MSTSC_Monitor --distpath "." --workpath "build" --specpath "." "completion_monitor.py"
if errorlevel 1 (
    echo Build failed. Check the errors above.
    if "%PAUSE_AT_END%"=="1" pause
    exit /b 1
)

echo Build complete: MSTSC_Monitor.exe
if "%PAUSE_AT_END%"=="1" pause
