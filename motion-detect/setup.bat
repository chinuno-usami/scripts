@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

echo ========================================
echo  motion-detect Windows setup
echo ========================================
echo.

rem ---- locate Python ----
set "PY="
call :FIND_PYTHON
if defined PY goto PYTHON_OK

echo [error] Python 3.10+ not found.
echo         Install from https://www.python.org/downloads/
echo         IMPORTANT: check "Add python.exe to PATH".
echo.
echo Trying winget install Python.Python.3.12 ...
where winget >nul 2>&1
if errorlevel 1 (
  echo [error] winget not available either.
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo [error] winget install failed.
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

rem Refresh PATH from Machine + User for this session, then re-detect.
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%A"
set "PY="
call :FIND_PYTHON
if not defined PY (
  echo [error] Python installed, but this shell still cannot find it.
  echo         Close this window and re-run setup.bat.
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

:PYTHON_OK
echo [ok] using: %PY%
%PY% --version
if errorlevel 1 (
  echo [error] Python launcher failed.
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

rem ---- create venv ----
if not exist ".venv\Scripts\python.exe" (
  echo [info] creating virtualenv .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [error] failed to create venv
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
  )
) else (
  echo [ok] virtualenv already exists
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [error] pip install failed
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)
echo [ok] Python packages installed

rem ---- portable ffmpeg ----
echo [info] ensuring portable ffmpeg ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\download_ffmpeg.ps1"
if errorlevel 1 (
  echo [error] ffmpeg setup failed
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

echo.
echo ========================================
echo  Setup complete.
echo  Next: drag a video onto detect.bat
echo  or double-click detect.bat
echo ========================================
if "%NO_PAUSE%"=="0" pause
endlocal
exit /b 0

:FIND_PYTHON
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
  if %errorlevel%==0 set "PY=py -3"
)
if defined PY goto :eof
where python >nul 2>&1
if %errorlevel%==0 (
  python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
  if %errorlevel%==0 set "PY=python"
)
goto :eof
