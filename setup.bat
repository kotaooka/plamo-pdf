@echo off
rem plamo-pdf setup: double-click this file once.
rem   1. creates .venv and installs Python packages
rem   2. finds llama-server (or downloads llama.cpp from GitHub)
rem   3. downloads the model and translates one sentence as a test
rem Non-interactive: setup.bat 8b   /  setup.bat 0.8b  /  setup.bat 8b 0.8b
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo === plamo-pdf setup ===
echo.

rem ---- 1. Python 3.10+
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 or later was not found.
  echo Install it from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^) and run this again.
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating .venv ...
  %PY% -m venv .venv || goto :fail
)
echo [1/3] Installing Python packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

rem ---- 2. choose model
if not "%~1"=="" (
  set "MODELS=%*"
  set "DEF=%~1"
  goto :run
)
echo.
echo Which model do you want to use?
echo   1: 8b    best quality   VRAM about 6.5 GB, download 4.9 GB
echo   2: 0.8b  light and fast VRAM about 1 GB,   download 0.6 GB
echo   3: both  (8b is used by default)
choice /c 123 /n /m "Select 1-3: "
if errorlevel 3 (
  set "MODELS=8b 0.8b"
  set "DEF=8b"
) else if errorlevel 2 (
  set "MODELS=0.8b"
  set "DEF=0.8b"
) else (
  set "MODELS=8b"
  set "DEF=8b"
)

:run
rem ---- 3. llama-server + model + test translation
for %%m in (%MODELS%) do (
  echo.
  echo [2/3] Preparing llama-server and model %%m ...
  ".venv\Scripts\python.exe" plamo_pdf.py --setup --model %%m || goto :fail
)
> model.txt echo %DEF%

echo.
echo [3/3] Done. Default model: %DEF% ^(saved in model.txt^)
echo Drag and drop PDF files onto plamo_pdf.bat to translate them.
pause
exit /b 0

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
