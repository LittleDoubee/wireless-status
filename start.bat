@echo off
chcp 65001 >nul
setlocal

rem ------------------------------------------------------------------
rem  WirelessStatus - silent launcher
rem
rem  This file is pure ASCII ON PURPOSE. cmd.exe reads .bat files as the
rem  console codepage (GBK on zh-CN Windows). A UTF-8 Chinese echo line
rem  gets decoded as GBK and the byte soup is re-split into commands, so
rem  the console spams "'...' is not recognized as an internal or
rem  external command". Keep every byte here ASCII.
rem
rem  Behaviour:
rem    - double-click = start the overlay, NO console window stays open
rem      and NO console flashes. Everything runs inside this hidden cmd.
rem    - errors are shown with a message box (msgbox below) instead of
rem      "pause": pause needs a console, and a console is exactly what we
rem      are trying to avoid.
rem    - every outcome goes to logs\start.log
rem ------------------------------------------------------------------

cd /d "%~dp0"

set "LOGDIR=%~dp0logs"
set "LOGFILE=%LOGDIR%\start.log"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul

echo. >> "%LOGFILE%"
echo [%DATE% %TIME%] start.bat invoked from %CD% >> "%LOGFILE%"

rem ---- 1. locate python --------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo [%DATE% %TIME%] ERROR python not in PATH >> "%LOGFILE%"
  call :fail "Python not found in PATH.^
Install Python 3.10+ and tick 'Add python.exe to PATH'.^
Log: %LOGFILE%"
  exit /b 1
)

rem ---- 2. pick pythonw.exe (same dir as python.exe) -----------------
rem python.exe would keep a console attached; pythonw.exe is the same
rem interpreter without one.
set "PYW="
for /f "delims=" %%P in ('where python') do (
  if not defined PYW (
    if exist "%%~dpPpythonw.exe" set "PYW=%%~dpPpythonw.exe"
  )
)
if not defined PYW (
  echo [%DATE% %TIME%] WARN pythonw.exe not found, falling back to python.exe >> "%LOGFILE%"
  set "PYW=python"
)

rem ---- 3. dependencies ---------------------------------------------
rem hidapi is the one that is commonly missing; check before launch so
rem the user gets a readable message instead of a silent no-show.
"%PYW%" -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo [%DATE% %TIME%] ERROR pywebview missing >> "%LOGFILE%"
  call :fail "pywebview is not installed.^
Run:  python -m pip install --no-deps pywebview^
Log: %LOGFILE%"
  exit /b 1
)

"%PYW%" -c "import hid" >nul 2>nul
if errorlevel 1 (
  echo [%DATE% %TIME%] WARN hidapi missing, mouse features disabled >> "%LOGFILE%"
  call :warn "hidapi is not installed.^
The window will still open but no mouse can be found.^
Run:  python -m pip install hidapi"
)

"%PYW%" -c "import win32gui" >nul 2>nul
if errorlevel 1 (
  echo [%DATE% %TIME%] WARN pywin32 missing, tray disabled >> "%LOGFILE%"
  call :warn "pywin32 is not installed - the tray icon will be missing.^
Run:  python -m pip install pywin32"
)

rem ---- 4. launch ---------------------------------------------------
rem "start "" ..." detaches, so this cmd exits immediately and no console
rem lingers. The GUI process owns its own (invisible) window.
echo [%DATE% %TIME%] launching %PYW% main.py >> "%LOGFILE%"
start "" "%PYW%" "%~dp0main.py"

exit /b 0

rem ------------------------------------------------------------------
rem  helpers: message box without a console
rem  PowerShell is guaranteed on Windows 7+; -WindowStyle Hidden keeps
rem  the console flash away.
rem ------------------------------------------------------------------
:fail
echo [%DATE% %TIME%] FAIL %* >> "%LOGFILE%"
call :msgbox "WirelessStatus" "%~1" 16
exit /b 0

:warn
echo [%DATE% %TIME%] WARN %* >> "%LOGFILE%"
call :msgbox "WirelessStatus" "%~1" 48
exit /b 0

:msgbox
powershell -NoProfile -WindowStyle Hidden -Command ^
  "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('%2','%1',$([System.Windows.Forms.MessageBoxButtons]::OK),[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null" >nul 2>nul
exit /b 0
