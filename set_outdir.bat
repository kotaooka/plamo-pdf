@echo off
rem Choose the folder where translated files are saved (stored in config.ini).
rem Cancel the dialog to keep the current folder or reset it to "same folder as the PDF".
setlocal
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0plamo_pdf.py" --choose-out-dir
pause
