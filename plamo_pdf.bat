@echo off
rem Drag and drop PDF files onto this file to translate them and open the results
rem (bilingual HTML and a PDF with the original layout, by default).
rem Output folder and formats are set in config.ini (set_outdir.bat chooses the folder).
rem Run setup.bat once first. The model chosen there is read from model.txt.
rem To change it, edit model.txt (8b or 0.8b) or run setup.bat again.
setlocal
set MODEL=8b
if exist "%~dp0model.txt" set /p MODEL=<"%~dp0model.txt"

chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Drag and drop PDF files onto plamo_pdf.bat.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0plamo_pdf.py" --model %MODEL% --open %*
pause
