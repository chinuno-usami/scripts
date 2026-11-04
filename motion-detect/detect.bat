@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem Auto-setup if needed
if not exist ".venv\Scripts\python.exe" goto NEED_SETUP
if not exist "tools\ffmpeg\bin\ffmpeg.exe" (
  where ffmpeg >nul 2>&1
  if errorlevel 1 goto NEED_SETUP
)
goto READY

:NEED_SETUP
echo [info] environment missing, running setup.bat ...
call "%~dp0setup.bat" --no-pause
if not exist ".venv\Scripts\python.exe" (
  echo [error] setup failed
  pause
  exit /b 1
)

:READY
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"

set "VIDEO=%~1"
if "%VIDEO%"=="" (
  echo Drag-and-drop a video onto this bat, or paste path below.
  set /p "VIDEO=Video path: "
)
if "%VIDEO%"=="" (
  echo [error] no video provided
  pause
  exit /b 1
)
if not exist "%VIDEO%" (
  echo [error] file not found: %VIDEO%
  pause
  exit /b 1
)

for %%I in ("%VIDEO%") do (
  set "NAME=%%~nI"
)
set "OUTDIR=%~dp0output\%NAME%"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo.
echo [info] input : %VIDEO%
echo [info] output: %OUTDIR%
echo.

"%VENV_PY%" "%~dp0detect_changes.py" "%VIDEO%" ^
  -o "%OUTDIR%\events.json" ^
  --export-frames "%OUTDIR%\frames" ^
  --score-csv "%OUTDIR%\scores.csv" ^
  --score-plot "%OUTDIR%\scores.png"

set "ERR=%errorlevel%"
echo.
if not "%ERR%"=="0" (
  echo [error] detection failed, code=%ERR%
) else (
  echo [ok] done
  echo      events : %OUTDIR%\events.json
  echo      frames : %OUTDIR%\frames\
  echo      scores : %OUTDIR%\scores.png
  explorer "%OUTDIR%"
)
pause
endlocal & exit /b %ERR%
